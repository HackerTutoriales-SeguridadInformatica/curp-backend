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

class CurpRequest(BaseModel):
    curp: str
    captcha: str

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
    return driver

@app.get("/")
async def root():
    return {"status": "ok", "mensaje": "Backend de CURP funcionando"}

@app.get("/captcha")
async def obtener_captcha(curp: str):
    """
    Abre el portal de la CURP, ingresa la CURP sin disparar onkeypress,
    y devuelve la imagen del captcha en base64.
    """
    driver = None
    try:
        driver = crear_driver()

        # ✅ Bloquear TODOS los alert() de JavaScript antes de cargar la página
        # Esto evita que la alerta "botón deshabilitado" bloquee el script
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                window.alert = function(){ return true; };
                window.confirm = function(){ return true; };
                window.prompt = function(){ return null; };
            """
        })

        driver.get(CURP_URL)
        time.sleep(3)

        # ✅ Verificar si hay alguna alerta abierta y cerrarla
        try:
            alert = driver.switch_to.alert
            alert.accept()
        except:
            pass

        # ✅ Llenar el campo de la CURP usando JavaScript para evitar el onkeypress
        # Esto evita que se dispare enviarforma() al escribir
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "strCurp"))
        )
        driver.execute_script(f"""
            var campo = document.getElementsByName('strCurp')[0];
            campo.value = '{curp.upper()}';
            campo.dispatchEvent(new Event('input', {{ bubbles: true }}));
            campo.dispatchEvent(new Event('change', {{ bubbles: true }}));
        """)

        time.sleep(2)

        # ✅ Verificar si hay alertas abiertas de nuevo
        try:
            alert = driver.switch_to.alert
            alert.accept()
        except:
            pass

        # ✅ Tomar screenshot de la imagen del captcha
        img_captcha = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "capt"))
        )

        img_base64 = img_captcha.screenshot_as_base64

        return {"captcha_img": img_base64}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener captcha: {str(e)}")
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass

@app.post("/descargar-curp")
async def descargar_curp(payload: CurpRequest):
    """
    Recibe la CURP y el captcha resuelto. Descarga el PDF.
    """
    driver = None
    try:
        driver = crear_driver()

        # Bloquear alerts
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                window.alert = function(){ return true; };
                window.confirm = function(){ return true; };
                window.prompt = function(){ return null; };
            """
        })

        driver.get(CURP_URL)
        time.sleep(3)

        # Cerrar alertas si hay
        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # Llenar CURP via JavaScript
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "strCurp"))
        )
        driver.execute_script(f"""
            var campo = document.getElementsByName('strCurp')[0];
            campo.value = '{payload.curp.upper()}';
            campo.dispatchEvent(new Event('input', {{ bubbles: true }}));
        """)

        # Llenar captcha via JavaScript
        driver.execute_script(f"""
            var campo = document.getElementsByName('codigo')[0];
            campo.value = '{payload.captcha.upper()}';
            campo.dispatchEvent(new Event('input', {{ bubbles: true }}));
        """)

        time.sleep(1)

        # Hacer clic en el botón Buscar
        driver.execute_script("enviar2();")

        time.sleep(4)

        # Cerrar alertas si hay
        try:
            driver.switch_to.alert.accept()
        except:
            pass

        # Buscar el enlace del PDF
        enlaces_pdf = driver.find_elements(By.XPATH, "//a[contains(@href, '.pdf') or contains(@onclick, 'pdf') or contains(@onclick, 'PDF')]")

        if not enlaces_pdf:
            enlaces_pdf = driver.find_elements(By.XPATH, "//input[contains(@value, 'Imprimir') or contains(@value, 'Descargar')] | //button[contains(text(), 'Imprimir') or contains(text(), 'Descargar')]")

        if not enlaces_pdf:
            raise Exception("No se encontró el botón para descargar el PDF.")

        url_pdf = enlaces_pdf[0].get_attribute('href')

        if not url_pdf:
            enlaces_pdf[0].click()
            time.sleep(3)
            raise Exception("El botón no tiene href directo.")

        # Descargar el PDF con las cookies
        session = requests.Session()
        for cookie in driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'])

        pdf_response = session.get(url_pdf)
        pdf_response.raise_for_status()

        return Response(
            content=pdf_response.content,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=CURP_{payload.curp}.pdf"}
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al descargar CURP: {str(e)}")
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
