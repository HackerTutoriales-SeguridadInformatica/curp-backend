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
import threading

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
_lock = threading.Lock()


class CurpRequest(BaseModel):
    curp: str
    captcha: str
    session_id: str


def crear_driver():
    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument('--window-size=1280,900')
    options.add_argument('--disable-popup-blocking')
    options.add_argument('--disable-gpu')
    options.add_argument('--disable-extensions')
    options.add_argument('--disable-background-networking')
    options.add_argument('--disable-sync')
    options.add_argument('--metrics-recording-only')
    options.add_argument('--mute-audio')
    options.add_argument('--no-first-run')
    options.add_argument('--safebrowsing-disable-auto-update')
    options.add_argument('--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter')
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    # NO usamos page_load_strategy='eager' porque cortaba el CAPTCHA

    driver = webdriver.Remote(command_executor=SELENIUM_URL, options=options)
    driver.set_page_load_timeout(30)
    driver.set_script_timeout(15)
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
    with _lock:
        for sid, info in sesiones_activas.items():
            if ahora - info["created_at"] > TIMEOUT_SESION_SEGUNDOS:
                try:
                    info["driver"].quit()
                except:
                    pass
                sesiones_a_borrar.append(sid)
        for sid in sesiones_a_borrar:
            del sesiones_activas[sid]


def aceptar_alerta(driver, timeout=0.5):
    try:
        WebDriverWait(driver, timeout).until(EC.alert_is_present())
        driver.switch_to.alert.accept()
        return True
    except:
        return False


@app.get("/")
async def root():
    return {"status": "ok", "sesiones_activas": len(sesiones_activas)}


@app.get("/captcha")
async def obtener_captcha(curp: str):
    limpiar_sesiones_viejas()
    curp = curp.strip().upper()

    driver = None
    t0 = time.time()
    try:
        driver = crear_driver()
        driver.get(CURP_URL)
        print(f"[TIEMPO] driver.get: {time.time()-t0:.2f}s")

        aceptar_alerta(driver, 1)

        # Esperar el campo CURP y llenarlo
        campo_curp = WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.NAME, "strCurp"))
        )
        driver.execute_script("""
            var campo = document.getElementsByName('strCurp')[0];
            campo.value = arguments[0];
            campo.dispatchEvent(new Event('input', { bubbles: true }));
            campo.dispatchEvent(new Event('change', { bubbles: true }));
        """, curp)
        print(f"[TIEMPO] llenar CURP: {time.time()-t0:.2f}s")

        # === ESPERAS PARA QUE EL CAPTCHA ESTÉ COMPLETAMENTE RENDERIZADO ===
        # 1. Que exista el elemento <img id="capt">
        img_captcha = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "capt"))
        )

        # 2. Que la imagen esté cargada COMPLETAMENTE
        WebDriverWait(driver, 8).until(
            lambda d: d.execute_script(
                "var i = document.getElementById('capt'); "
                "return i && i.complete && i.naturalWidth > 0 && i.naturalHeight > 0;"
            )
        )

        # 3. Que el elemento tenga dimensiones visibles reales
        WebDriverWait(driver, 8).until(
            lambda d: d.execute_script(
                "var i = document.getElementById('capt'); "
                "if (!i) return false; "
                "var r = i.getBoundingClientRect(); "
                "return r.width > 0 && r.height > 0;"
            )
        )

        # 4. Scroll hacia el CAPTCHA para que esté 100% visible en el viewport
        driver.execute_script("""
            var i = document.getElementById('capt');
            if (i) {
                i.scrollIntoView({block: 'center', inline: 'center'});
            }
        """)

        # 5. Esperar a que el scroll se asiente y Chrome repinte
        time.sleep(0.4)

        # 6. Esperar a que no haya animaciones ni transiciones activas
        WebDriverWait(driver, 4).until(
            lambda d: d.execute_script("""
                var i = document.getElementById('capt');
                if (!i) return false;
                var s = window.getComputedStyle(i);
                return parseFloat(s.opacity) >= 0.99 && s.visibility === 'visible' && s.display !== 'none';
            """)
        )

        aceptar_alerta(driver, 0.3)
        print(f"[TIEMPO] captcha listo para screenshot: {time.time()-t0:.2f}s")

        # === Screenshot del elemento ===
        img_base64 = img_captcha.screenshot_as_base64

        # Validación: si el base64 es muy pequeño, la imagen puede estar cortada
        if len(img_base64) < 2000:
            print(f"[ADVERTENCIA] Screenshot del captcha muy pequeño ({len(img_base64)} chars). Reintentando...")
            driver.execute_script("document.getElementById('capt').scrollIntoView({block:'center'});")
            time.sleep(0.5)
            img_base64 = img_captcha.screenshot_as_base64

        print(f"[TIEMPO] screenshot captcha: {time.time()-t0:.2f}s (base64: {len(img_base64)} chars)")

        # Capturar la estructura del formulario para replicar el POST después
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
                    // no incluir botones
                } else {
                    data[el.name] = el.value;
                }
            }
            return { action: action, method: method, fields: data };
        """)

        session_id = str(uuid.uuid4())
        with _lock:
            sesiones_activas[session_id] = {
                "driver": driver,
                "curp": curp,
                "created_at": time.time(),
                "form_info": form_fields,
            }

        print(f"[TIEMPO] TOTAL captcha: {time.time()-t0:.2f}s")
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

    t0 = time.time()
    try:
        aceptar_alerta(driver, 0.3)

        # === 1. Rellenar CAPTCHA ===
        driver.execute_script("""
            var campo = document.getElementsByName('codigo')[0];
            if (campo) {
                campo.value = arguments[0];
                campo.dispatchEvent(new Event('input', { bubbles: true }));
                campo.dispatchEvent(new Event('change', { bubbles: true }));
            }
        """, captcha)

        # === 2. Enviar formulario con enviar2() y esperar el botón "Imprimir Formato" ===
        driver.execute_script("document.body.setAttribute('data-pre-envio', '1');")
        driver.execute_script("enviar2();")
        print(f"[TIEMPO] enviar2 ejecutado: {time.time()-t0:.2f}s")

        try:
            boton_imprimir = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.NAME, "sbmBuscar"))
            )
            print(f"[TIEMPO] botón sbmBuscar detectado: {time.time()-t0:.2f}s")
        except Exception:
            aceptar_alerta(driver, 0.5)
            boton_imprimir = None

        aceptar_alerta(driver, 0.3)

        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text[:500]
            print(f"[DIAGNÓSTICO] Texto tras enviar: {body_text}")
        except:
            body_text = ""

        if not boton_imprimir:
            raise HTTPException(
                status_code=400,
                detail="No se encontró el botón 'Imprimir Formato'. Verifica que el CAPTCHA sea correcto. "
                       "Texto de la página: " + body_text[:300]
            )

        # === 3. Capturar el estado actual del formulario (por si el server agregó campos) ===
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
                    // no incluir
                } else {
                    data[el.name] = el.value;
                }
            }
            return { action: action, method: method, fields: data };
        """)
        print(f"[TIEMPO] capturar form actual: {time.time()-t0:.2f}s")

        # === 4. POST directo con requests (evita el clic del botón) ===
        cookies_dict = {c['name']: c['value'] for c in driver.get_cookies()}

        action_url = (form_actual or form_info).get("action") or driver.current_url
        if action_url and not action_url.startswith("http"):
            action_url = urljoin(driver.current_url, action_url)

        fields = dict((form_actual or form_info).get("fields") or {})
        fields['codigo'] = captcha
        fields['strCurp'] = curp
        fields['sbmBuscar'] = ' Imprimir Formato '

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/pdf,text/html,application/xhtml+xml,*/*;q=0.8',
            'Accept-Language': 'es-MX,es;q=0.9,en;q=0.8',
            'Referer': driver.current_url,
            'Origin': 'https://consultas.curp.gob.mx',
            'Content-Type': 'application/x-www-form-urlencoded',
        }

        session = requests.Session()
        for k, v in cookies_dict.items():
            session.cookies.set(k, v)

        print(f"[TIEMPO] POST a {action_url}: {time.time()-t0:.2f}s")
        resp = session.post(action_url, data=fields, headers=headers, timeout=30, allow_redirects=True)
        print(f"[TIEMPO] POST completado: {time.time()-t0:.2f}s")

        contenido = resp.content
        content_type = resp.headers.get('Content-Type', '')
        es_pdf = contenido[:4] == b'%PDF'

        print(f"[DIAGNÓSTICO] POST status: {resp.status_code} | Content-Type: {content_type}")
        print(f"[DIAGNÓSTICO] Es PDF: {es_pdf} | Tamaño: {len(contenido)} bytes")

        # === 5. Fallback: si no es PDF, buscar embed/iframe ===
        if not es_pdf:
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
                    resp2 = session.get(pdf_url, headers=headers, timeout=20)
                    if resp2.content[:4] == b'%PDF':
                        contenido = resp2.content
                        content_type = resp2.headers.get('Content-Type', '')
                        es_pdf = True
                        print(f"[DIAGNÓSTICO] PDF obtenido desde embed/iframe")
            except Exception as e:
                print(f"[DIAGNÓSTICO] Error en fallback: {e}")

        # === 6. Limpiar sesión ===
        try:
            driver.quit()
        except:
            pass
        with _lock:
            if payload.session_id in sesiones_activas:
                del sesiones_activas[payload.session_id]

        print(f"[TIEMPO] TOTAL descargar-curp: {time.time()-t0:.2f}s")

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
        with _lock:
            if payload.session_id in sesiones_activas:
                del sesiones_activas[payload.session_id]
        raise HTTPException(status_code=500, detail=f"Error al descargar CURP: {str(e)}")
