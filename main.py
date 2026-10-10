from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from urllib.parse import urljoin
import requests
import os
import time
import uuid

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SELENIUM_URL = os.getenv("SELENIUM_URL", "http://localhost:4444")
CURP_URL = "https://consultas.curp.gob.mx/CurpSP/consulinterCurSP.do"

sesiones_activas = {}
TIMEOUT_SESION_SEGUNDOS = 300


class CurpRequest(BaseModel):
    curp: str
    captcha: str
    session_id: str


def crear_driver():
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--disable-popup-blocking')
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    driver = webdriver.Remote(command_executor=SELENIUM_URL, options=options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            window.alert = function(){ return true; };
            window.confirm = function(){ return true; };
            window.prompt = function(){ return null; };
        """
    })
    return driver


def limpiar_sesiones_viejas():
    ahora = time.time()
    sesiones_a_borrar = []
    for sid, info in sesiones_activas.items():
        if ahora - info["created_at"] > TIMEOUT_SESION_SEGUNDOS:
            try:
                info["driver"].quit()
            except:
                pass
            sesiones_a_borrar.append(sid)
    for sid in sesiones_a_borrar:
        del sesiones_activas[sid]


@app.get("/")
async def root():
    return {"status": "ok", "sesiones_activas": len(sesiones_activas)}


@app.get("/captcha")
async def obtener_captcha(curp: str):
    limpiar_sesiones_viejas()
    curp = curp.strip().upper()

    driver = None
    try:
        driver = crear_driver()
        driver.get(CURP_URL)
        time.sleep(3)

        try:
            driver.switch_to.alert.accept()
        except:
            pass

        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "strCurp"))
        )
        driver.execute_script("""
            var campo = document.getElementsByName('strCurp')[0];
            campo.value = arguments[0];
            campo.dispatchEvent(new Event('input', { bubbles: true }));
        """, curp)

        time.sleep(2)

        try:
            driver.switch_to.alert.accept()
        except:
            pass

        img_captcha = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "capt"))
        )
        img_base64 = img_captcha.screenshot_as_base64

        session_id = str(uuid.uuid4())
        sesiones_activas[session_id] = {
            "driver": driver,
            "curp": curp,
            "created_at": time.time()
        }

        return {
            "captcha_img": img_base64,
            "session_id": session_id
        }

    except Exception as e:
        if driver:
            try:
                driver.quit()
            except:
                pass
        raise HTTPException(status_code=500, detail=f"Error al obtener captcha: {str(e)}")


@app.post("/descargar-curp")
async def descargar_curp(payload: CurpRequest):
    limpiar_sesiones_viejas()
    curp = payload.curp.strip().upper()
    captcha = payload.captcha.strip().upper()

    if payload.session_id not in sesiones_activas:
        raise HTTPException(status_code=400, detail="La sesión ha expirado. Vuelve a solicitar el CAPTCHA.")

    info = sesiones_activas[payload.session_id]
    driver = info["driver"]

    try:
        # Aceptar cualquier alerta pendiente
        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # === 1. Rellenar el CAPTCHA y enviar ===
        driver.execute_script("""
            var campo = document.getElementsByName('codigo')[0];
            campo.value = arguments[0];
            campo.dispatchEvent(new Event('input', { bubbles: true }));
        """, captcha)

        time.sleep(1)

        try:
            driver.switch_to.alert.accept()
        except:
            pass

        driver.execute_script("enviar2();")
        time.sleep(4)

        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # === 2. Diagnóstico del texto de la página ===
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text[:800]
            print(f"[DIAGNÓSTICO] Texto de la página tras enviar: {body_text}")
        except Exception as e:
            body_text = ""
            print(f"[DIAGNÓSTICO] Error al leer body: {e}")

        # === 3. Buscar el botón "Imprimir Formato" por name="sbmBuscar" ===
        print("[DIAGNÓSTICO] Buscando botón 'Imprimir Formato' (name='sbmBuscar')...")

        boton_imprimir = None
        try:
            boton_imprimir = WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.NAME, "sbmBuscar"))
            )
            print(f"[DIAGNÓSTICO] Botón encontrado -> "
                  f"name={boton_imprimir.get_attribute('name')}, "
                  f"value={boton_imprimir.get_attribute('value')}, "
                  f"onclick={boton_imprimir.get_attribute('onclick')}")
        except Exception as e:
            print(f"[DIAGNÓSTICO] No se encontró por name='sbmBuscar': {e}")
            # Fallback por XPATH value
            try:
                boton_imprimir = driver.find_element(
                    By.XPATH,
                    "//input[@type='button' and contains(@value, 'Imprimir Formato')]"
                )
                print(f"[DIAGNÓSTICO] Botón encontrado por XPATH value: {boton_imprimir.get_attribute('value')}")
            except:
                pass
            # Fallback por onclick
            if not boton_imprimir:
                try:
                    boton_imprimir = driver.find_element(
                        By.XPATH, "//input[@onclick='enviar()']"
                    )
                    print(f"[DIAGNÓSTICO] Botón encontrado por onclick='enviar()'")
                except:
                    pass

        if not boton_imprimir:
            raise HTTPException(
                status_code=400,
                detail="No se encontró el botón 'Imprimir Formato'. Verifica que el CAPTCHA sea correcto. "
                       "Texto de la página: " + body_text[:300]
            )

        # === 4. Hacer clic y detectar nueva ventana ===
        ventanas_antes = set(driver.window_handles)
        print(f"[DIAGNÓSTICO] Ventanas antes del clic: {len(ventanas_antes)}")

        # Click por JavaScript para evitar intercepciones
        driver.execute_script("arguments[0].click();", boton_imprimir)

        # Esperar a que se abra la nueva ventana o se genere el PDF
        time.sleep(5)

        try:
            driver.switch_to.alert.accept()
        except:
            pass

        ventanas_despues = set(driver.window_handles)
        ventanas_nuevas = ventanas_despues - ventanas_antes
        print(f"[DIAGNÓSTICO] Ventanas después del clic: {len(ventanas_despues)}, nuevas: {len(ventanas_nuevas)}")

        url_pdf = None

        if ventanas_nuevas:
            nueva_ventana = list(ventanas_nuevas)[0]
            driver.switch_to.window(nueva_ventana)
            time.sleep(2)
            url_pdf = driver.current_url
            print(f"[DIAGNÓSTICO] Nueva ventana detectada. URL: {url_pdf}")
        else:
            url_pdf = driver.current_url
            print(f"[DIAGNÓSTICO] Sin nueva ventana. URL actual: {url_pdf}")

        # === 5. Buscar si hay un <embed>, <iframe> u <object> con el PDF ===
        try:
            pdf_embed = driver.execute_script("""
                function buscarPdf(doc, depth) {
                    if (depth > 3) return null;
                    try {
                        var embed = doc.querySelector('embed[type="application/pdf"], embed[src*=".pdf"], embed[src*="pdf"]');
                        if (embed && embed.src) return { tipo: 'embed', src: embed.src };
                        var obj = doc.querySelector('object[type="application/pdf"], object[data*=".pdf"]');
                        if (obj && obj.data) return { tipo: 'object', src: obj.data };
                        var iframe = doc.querySelector('iframe');
                        if (iframe) {
                            if (iframe.src && (iframe.src.toLowerCase().includes('.pdf') || iframe.src.toLowerCase().includes('pdf'))) {
                                return { tipo: 'iframe', src: iframe.src };
                            }
                            try {
                                var inner = buscarPdf(iframe.contentDocument, depth + 1);
                                if (inner) return inner;
                            } catch (e) {}
                        }
                    } catch (e) {}
                    return null;
                }
                return buscarPdf(document, 0);
            """)
            print(f"[DIAGNÓSTICO] Búsqueda de PDF embebido: {pdf_embed}")
            if pdf_embed and pdf_embed.get('src'):
                url_pdf = pdf_embed['src']
                print(f"[DIAGNÓSTICO] URL del PDF desde {pdf_embed['tipo']}: {url_pdf}")
        except Exception as e:
            print(f"[DIAGNÓSTICO] Error buscando PDF embebido: {e}")

        # Si la URL es relativa, construir absoluta
        if url_pdf and not url_pdf.startswith("http"):
            url_pdf = urljoin(driver.current_url, url_pdf)
            print(f"[DIAGNÓSTICO] URL absoluta construida: {url_pdf}")

        print(f"[DIAGNÓSTICO] URL final para descargar: {url_pdf}")

        # === 6. Descargar con cookies de Selenium ===
        session = requests.Session()
        for cookie in driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'])

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/pdf,application/octet-stream,*/*',
            'Referer': driver.current_url,
        }

        contenido = b""
        content_type = ""
        es_pdf = False

        try:
            pdf_response = session.get(url_pdf, headers=headers, timeout=30, allow_redirects=True)
            print(f"[DIAGNÓSTICO] GET final URL: {pdf_response.url}")
            print(f"[DIAGNÓSTICO] Status: {pdf_response.status_code}")
            print(f"[DIAGNÓSTICO] Content-Type: {pdf_response.headers.get('Content-Type', '')}")
            contenido = pdf_response.content
            content_type = pdf_response.headers.get('Content-Type', '')
            es_pdf = contenido[:4] == b'%PDF'
            print(f"[DIAGNÓSTICO] Es PDF válido: {es_pdf} | Tamaño: {len(contenido)} bytes")
            print(f"[DIAGNÓSTICO] Primeros 300 bytes: {contenido[:300]}")
        except Exception as e:
            print(f"[DIAGNÓSTICO] Error en requests.get: {e}")

        # === 7. Si no es PDF, último intento: usar el navegador para obtener el contenido ===
        if not es_pdf:
            print("[DIAGNÓSTICO] No es PDF con requests. Intentando con Selenium...")
            try:
                # Si la URL termina en .pdf o similar, traer el blob vía JS
                pdf_base64 = driver.execute_script("""
                    try {
                        // Buscar en <embed> el contenido
                        var embed = document.querySelector('embed[type="application/pdf"]');
                        if (embed) return embed.src;
                        var iframe = document.querySelector('iframe');
                        if (iframe) return iframe.src;
                    } catch(e) { return null; }
                    return null;
                """)
                print(f"[DIAGNÓSTICO] URL embebida en el navegador: {pdf_base64}")
            except Exception as e:
                print(f"[DIAGNÓSTICO] Error intentando Selenium: {e}")

        # === 8. Cerrar sesión ===
        try:
            driver.quit()
        except:
            pass
        if payload.session_id in sesiones_activas:
            del sesiones_activas[payload.session_id]

        if not es_pdf:
            texto_recibido = contenido.decode('utf-8', errors='replace')[:500]
            raise HTTPException(
                status_code=500,
                detail=f"El archivo descargado NO es un PDF (Content-Type: {content_type}). "
                       f"Primeros 500 caracteres: {texto_recibido}"
            )

        return Response(
            content=contenido,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=CURP_{curp}.pdf"}
        )

    except HTTPException:
        raise
    except Exception as e:
        try:
            driver.quit()
        except:
            pass
        if payload.session_id in sesiones_activas:
            del sesiones_activas[payload.session_id]
        raise HTTPException(status_code=500, detail=f"Error al descargar CURP: {str(e)}")
