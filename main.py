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

        # === Capturar TODOS los campos del formulario para replicar el POST después ===
        form_fields = driver.execute_script("""
            var form = document.curpForma2 || document.forms['curpForma2'] || document.querySelector('form');
            if (!form) return null;
            var data = {};
            var action = form.action || window.location.href;
            var method = (form.method || 'POST').toUpperCase();
            var elements = form.querySelectorAll('input, select, textarea');
            for (var i = 0; i < elements.length; i++) {
                var el = elements[i];
                if (!el.name) continue;
                if (el.type === 'checkbox' || el.type === 'radio') {
                    if (el.checked) data[el.name] = el.value;
                } else if (el.type === 'submit' || el.type === 'button' || el.type === 'image') {
                    // no incluir
                } else {
                    data[el.name] = el.value;
                }
            }
            return { action: action, method: method, fields: data };
        """)
        print(f"[DIAGNÓSTICO] Formulario capturado: {form_fields}")

        session_id = str(uuid.uuid4())
        sesiones_activas[session_id] = {
            "driver": driver,
            "curp": curp,
            "created_at": time.time(),
            "form_info": form_fields,
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
    form_info = info.get("form_info") or {}

    try:
        # Aceptar cualquier alerta pendiente
        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # === PASO 1: Rellenar el CAPTCHA en el navegador (por si acaso) ===
        driver.execute_script("""
            var campo = document.getElementsByName('codigo')[0];
            if (campo) {
                campo.value = arguments[0];
                campo.dispatchEvent(new Event('input', { bubbles: true }));
            }
        """, captcha)
        time.sleep(1)

        # === PASO 2: Enviar el formulario con el CAPTCHA (enviar2) ===
        print("[DIAGNÓSTICO] Enviando formulario con CAPTCHA (enviar2)...")
        driver.execute_script("enviar2();")
        time.sleep(4)

        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # === PASO 3: Diagnóstico del texto de la página ===
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text[:800]
            print(f"[DIAGNÓSTICO] Texto de la página tras enviar CAPTCHA: {body_text}")
        except Exception as e:
            body_text = ""
            print(f"[DIAGNÓSTICO] Error al leer body: {e}")

        # === PASO 4: Capturar el estado ACTUAL del formulario (con los campos que puso el servidor) ===
        form_actual = driver.execute_script("""
            var form = document.curpForma2 || document.forms['curpForma2'] || document.querySelector('form');
            if (!form) return null;
            var data = {};
            var action = form.action || window.location.href;
            var method = (form.method || 'POST').toUpperCase();
            var elements = form.querySelectorAll('input, select, textarea');
            for (var i = 0; i < elements.length; i++) {
                var el = elements[i];
                if (!el.name) continue;
                if (el.type === 'checkbox' || el.type === 'radio') {
                    if (el.checked) data[el.name] = el.value;
                } else if (el.type === 'submit' || el.type === 'button' || el.type === 'image') {
                    // no incluir botones
                } else {
                    data[el.name] = el.value;
                }
            }
            return { action: action, method: method, fields: data };
        """)
        print(f"[DIAGNÓSTICO] Formulario actual: {form_actual}")

        # === PASO 5: Detectar el botón "Imprimir Formato" ===
        print("[DIAGNÓSTICO] Buscando botón 'Imprimir Formato' (name='sbmBuscar')...")
        boton_imprimir = None
        try:
            boton_imprimir = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.NAME, "sbmBuscar"))
            )
            print(f"[DIAGNÓSTICO] Botón encontrado -> value={boton_imprimir.get_attribute('value')}, "
                  f"onclick={boton_imprimir.get_attribute('onclick')}")
        except Exception as e:
            print(f"[DIAGNÓSTICO] No se encontró por name='sbmBuscar': {e}")

        if not boton_imprimir:
            raise HTTPException(
                status_code=400,
                detail="No se encontró el botón 'Imprimir Formato'. Verifica que el CAPTCHA sea correcto. "
                       "Texto de la página: " + body_text[:300]
            )

        # === PASO 6: En lugar de hacer clic, hacer el POST directamente con requests ===
        # Necesitamos las cookies de Selenium y los campos del formulario actual
        cookies_dict = {c['name']: c['value'] for c in driver.get_cookies()}
        print(f"[DIAGNÓSTICO] Cookies: {list(cookies_dict.keys())}")

        # El action del formulario (a dónde se hace el POST)
        action_url = (form_actual or form_info).get("action") or driver.current_url
        if action_url and not action_url.startswith("http"):
            action_url = urljoin(driver.current_url, action_url)
        print(f"[DIAGNÓSTICO] URL de acción del formulario: {action_url}")

        # Campos del formulario
        fields = dict((form_actual or form_info).get("fields") or {})
        # Asegurar que el CAPTCHA esté actualizado
        fields['codigo'] = captcha
        fields['strCurp'] = curp

        # Asegurar que se incluya el botón "Imprimir Formato" para que el servidor
        # sepa que queremos la impresión (el name es sbmBuscar)
        fields['sbmBuscar'] = ' Imprimir Formato '

        print(f"[DIAGNÓSTICO] Campos a enviar: {list(fields.keys())}")

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'es-MX,es;q=0.9,en;q=0.8',
            'Referer': driver.current_url,
            'Origin': 'https://consultas.curp.gob.mx',
            'Content-Type': 'application/x-www-form-urlencoded',
        }

        # Hacer el POST con requests
        print("[DIAGNÓSTICO] Haciendo POST con requests...")
        session = requests.Session()
        for k, v in cookies_dict.items():
            session.cookies.set(k, v)

        resp = session.post(action_url, data=fields, headers=headers, timeout=30, allow_redirects=True)
        print(f"[DIAGNÓSTICO] POST status: {resp.status_code}")
        print(f"[DIAGNÓSTICO] POST final URL: {resp.url}")
        print(f"[DIAGNÓSTICO] POST Content-Type: {resp.headers.get('Content-Type', '')}")

        contenido = resp.content
        content_type = resp.headers.get('Content-Type', '')
        es_pdf = contenido[:4] == b'%PDF'

        print(f"[DIAGNÓSTICO] Es PDF: {es_pdf} | Tamaño: {len(contenido)} bytes")
        print(f"[DIAGNÓSTICO] Primeros 300 bytes: {contenido[:300]}")

        # === PASO 7: Si no es PDF, intentar con el navegador (por si el servidor abrió algo en otra ventana) ===
        if not es_pdf:
            print("[DIAGNÓSTICO] No es PDF con POST. Revisando si el navegador tiene algo...")
            # Buscar en el navegador algún <embed>/<iframe> con el PDF
            try:
                pdf_url = driver.execute_script("""
                    var embed = document.querySelector('embed[type="application/pdf"], embed[src*=".pdf"]');
                    if (embed && embed.src) return embed.src;
                    var obj = document.querySelector('object[type="application/pdf"]');
                    if (obj && obj.data) return obj.data;
                    var iframe = document.querySelector('iframe');
                    if (iframe && iframe.src) return iframe.src;
                    return null;
                """)
                if pdf_url:
                    if not pdf_url.startswith("http"):
                        pdf_url = urljoin(driver.current_url, pdf_url)
                    print(f"[DIAGNÓSTICO] PDF embebido en navegador: {pdf_url}")
                    resp2 = session.get(pdf_url, headers=headers, timeout=30)
                    if resp2.content[:4] == b'%PDF':
                        contenido = resp2.content
                        content_type = resp2.headers.get('Content-Type', '')
                        es_pdf = True
                        print(f"[DIAGNÓSTICO] ¡PDF obtenido desde embebido! Tamaño: {len(contenido)}")
            except Exception as e:
                print(f"[DIAGNÓSTICO] Error buscando PDF en navegador: {e}")

        # === PASO 8: Cerrar sesión ===
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
