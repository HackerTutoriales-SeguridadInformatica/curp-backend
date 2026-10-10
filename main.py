from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
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
    captcha = payload.captcha.strip()  # ← CAMBIO: no forzar mayúsculas

    if payload.session_id not in sesiones_activas:
        raise HTTPException(status_code=400, detail="La sesión ha expirado. Vuelve a solicitar el CAPTCHA.")

    info = sesiones_activas[payload.session_id]
    driver = info["driver"]

    try:
        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # ✅ Enviar la CURP en mayúsculas y el CAPTCHA tal cual
        driver.execute_script("""
            document.ejemploForma2.strCurp.value = arguments[0];
            document.ejemploForma2.codigo.value = arguments[1];
        """, curp, captcha)

        time.sleep(1)

        try:
            driver.switch_to.alert.accept()
        except:
            pass

        driver.execute_script("document.ejemploForma2.submit();")
        time.sleep(5)

        try:
            driver.switch_to.alert.accept()
        except:
            pass

        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text[:800]
            print(f"[DIAGNOSTICO] URL actual: {driver.current_url}")
            print(f"[DIAGNOSTICO] Texto: {body_text}")
        except Exception as e:
            print(f"[DIAGNOSTICO] Error al leer body: {e}")

        enlaces_candidatos = driver.find_elements(By.XPATH,
            "//a[contains(@href, '.pdf') or contains(@href, 'pdf') "
            "or contains(@href, 'Imprimir') or contains(@href, 'imprimir') "
            "or contains(@href, 'Constancia') or contains(@href, 'constancia') "
            "or contains(@onclick, 'pdf') or contains(@onclick, 'PDF') "
            "or contains(@onclick, 'imprimir') or contains(@onclick, 'Imprimir')]"
        )

        print(f"[DIAGNOSTICO] Enlaces encontrados: {len(enlaces_candidatos)}")
        for i, enlace in enumerate(enlaces_candidatos):
            try:
                print(f"  [{i}] texto='{enlace.text}' href='{enlace.get_attribute('href')}' onclick='{enlace.get_attribute('onclick')}'")
            except:
                pass

        if not enlaces_candidatos:
            raise HTTPException(
                status_code=400,
                detail="No se encontró ningún enlace de descarga. Texto de la página: " + body_text[:400]
            )

        url_pdf = None
        for enlace in enlaces_candidatos:
            href = enlace.get_attribute('href')
            if href and href != "javascript:void(0)" and not href.startswith("javascript:"):
                url_pdf = href
                break

        if not url_pdf:
            enlaces_candidatos[0].click()
            time.sleep(3)
            url_pdf = driver.current_url
            print(f"[DIAGNOSTICO] URL tras clic: {url_pdf}")

        print(f"[DIAGNOSTICO] URL final del PDF: {url_pdf}")

        session = requests.Session()
        for cookie in driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'])

        pdf_response = session.get(url_pdf, timeout=30)
        pdf_response.raise_for_status()

        contenido = pdf_response.content
        content_type = pdf_response.headers.get('Content-Type', '')

        es_pdf = contenido[:4] == b'%PDF'

        print(f"[DIAGNOSTICO] Content-Type: {content_type}")
        print(f"[DIAGNOSTICO] Es PDF: {es_pdf}")
        print(f"[DIAGNOSTICO] Tamaño: {len(contenido)} bytes")

        try:
            driver.quit()
        except:
            pass
        del sesiones_activas[payload.session_id]

        if not es_pdf:
            texto_recibido = contenido.decode('utf-8', errors='replace')[:500]
            raise HTTPException(
                status_code=500,
                detail=f"El archivo descargado NO es un PDF (Content-Type: {content_type}). Primeros 500 caracteres: {texto_recibido}"
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
