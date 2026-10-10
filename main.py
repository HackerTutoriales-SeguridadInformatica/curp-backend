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
import base64

app = FastAPI()

# ✅ Permitir CORS para que tu panel en Vercel pueda llamar al backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SELENIUM_URL = os.getenv("SELENIUM_URL", "http://localhost:4444")
CURP_URL = "https://consultas.curp.gob.mx/CurpSP/consulinterCurSP.do"

# ✅ Sesiones activas: {session_id: {"driver": ..., "created_at": timestamp}}
sesiones_activas = {}
TIMEOUT_SESION_SEGUNDOS = 300  # 5 minutos máximo por sesión

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
    # Bloquear alerts
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            window.alert = function(){ return true; };
            window.confirm = function(){ return true; };
            window.prompt = function(){ return null; };
        """
    })
    return driver

def limpiar_sesiones_viejas():
    """Cierra drivers que llevan más de TIMEOUT_SESION_SEGUNDOS."""
    ahora = time.time()
    sessiones_a_borrar = []
    for sid, info in sesiones_activas.items():
        if ahora - info["created_at"] > TIMEOUT_SESION_SEGUNDOS:
            try:
                info["driver"].quit()
            except:
                pass
            sessiones_a_borrar.append(sid)
    for sid in sessiones_a_borrar:
        del sesiones_activas[sid]

@app.get("/")
async def root():
    return {"status": "ok", "mensaje": "Backend de CURP funcionando", "sesiones_activas": len(sesiones_activas)}

@app.get("/captcha")
async def obtener_captcha(curp: str):
    """
    Abre el portal, ingresa la CURP, guarda la sesión de Selenium en memoria
    y devuelve la imagen del captcha + el session_id.
    """
    limpiar_sesiones_viejas()

    driver = None
    try:
        driver = crear_driver()
        driver.get(CURP_URL)
        time.sleep(3)

        # Cerrar alertas si hay
        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # Llenar la CURP vía JavaScript (evita onkeypress)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "strCurp"))
        )
        driver.execute_script(f"""
            var campo = document.getElementsByName('strCurp')[0];
            campo.value = '{curp.upper()}';
            campo.dispatchEvent(new Event('input', {{ bubbles: true }}));
        """)

        time.sleep(2)

        # Cerrar alertas de nuevo
        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # Tomar screenshot del captcha
        img_captcha = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "capt"))
        )
        img_base64 = img_captcha.screenshot_as_base64

        # ✅ GUARDAR la sesión para reutilizarla en /descargar-curp
        session_id = str(uuid.uuid4())
        sesiones_activas[session_id] = {
            "driver": driver,
            "curp": curp.upper(),
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
    """
    Recibe la CURP, el captcha resuelto y el session_id.
    Reutiliza el driver guardado para enviar el formulario y descargar el PDF.
    """
    limpiar_sesiones_viejas()

    if payload.session_id not in sesiones_activas:
        raise HTTPException(status_code=400, detail="La sesión ha expirado. Vuelve a solicitar el CAPTCHA.")

    info = sesiones_activas[payload.session_id]
    driver = info["driver"]

    try:
        # Cerrar alertas si hay
        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # Llenar el captcha vía JavaScript
        driver.execute_script(f"""
            var campo = document.getElementsByName('codigo')[0];
            campo.value = '{payload.captcha.upper()}';
            campo.dispatchEvent(new Event('input', {{ bubbles: true }}));
        """)

        time.sleep(1)

        # Cerrar alertas
        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # Hacer clic en el botón Buscar via enviar2()
        driver.execute_script("enviar2();")

        time.sleep(4)

        # Cerrar alertas
        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # Verificar si la página de resultados cargó
        # Buscar enlaces o botones de descarga del PDF
        enlaces_pdf = driver.find_elements(By.XPATH, "//a[contains(@href, '.pdf') or contains(@onclick, 'pdf') or contains(@onclick, 'PDF') or contains(@href, 'imprimir') or contains(@href, 'Imprimir')]")

        if not enlaces_pdf:
            enlaces_pdf = driver.find_elements(By.XPATH, "//input[contains(@value, 'Imprimir') or contains(@value, 'Descargar') or contains(@value, 'Constancia')] | //button[contains(text(), 'Imprimir') or contains(text(), 'Descargar') or contains(text(), 'Constancia')]")

        if not enlaces_pdf:
            # Verificar si hay mensaje de error en la página
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
                if "incorrecto" in body_text.lower() or "inválido" in body_text.lower() or "error" in body_text.lower():
                    raise HTTPException(status_code=400, detail="CURP o CAPTCHA incorrecto. Intenta de nuevo.")
            except HTTPException:
                raise
            except:
                pass
            raise Exception("No se encontró el botón para descargar el PDF. Verifica el CAPTCHA e intenta de nuevo.")

        url_pdf = enlaces_pdf[0].get_attribute('href')

        # Si no tiene href, hacer clic y buscar el enlace resultante
        if not url_pdf:
            enlaces_pdf[0].click()
            time.sleep(3)
            url_pdf = driver.current_url

        # Descargar el PDF usando las cookies de la sesión
        session = requests.Session()
        for cookie in driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'])

        pdf_response = session.get(url_pdf)
        pdf_response.raise_for_status()

        # ✅ Cerrar la sesión después de usarla
        try:
            driver.quit()
        except:
            pass
        del sesiones_activas[payload.session_id]

        return Response(
            content=pdf_response.content,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=CURP_{payload.curp}.pdf"}
        )

    except HTTPException:
        # Re-lanzar HTTPExceptions sin cerrar el driver (para que el usuario reintente con otro captcha)
        raise
    except Exception as e:
        # Cerrar driver en caso de error fatal
        try:
            driver.quit()
        except:
            pass
        if payload.session_id in sesiones_activas:
            del sesiones_activas[payload.session_id]
        raise HTTPException(status_code=500, detail=f"Error al descargar CURP: {str(e)}")
