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
    Abre el portal de la CURP, ingresa la CURP y devuelve la imagen del captcha en base64.
    """
    driver = None
    try:
        driver = crear_driver()
        driver.get(CURP_URL)
        time.sleep(3)

        # 1. Llenar el campo de la CURP
        campo_curp = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "strCurp"))
        )
        campo_curp.clear()
        campo_curp.send_keys(curp.upper())

        time.sleep(2)

        # 2. Tomar screenshot de la imagen del captcha
        img_captcha = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "capt"))
        )

        # Hacer screenshot del elemento de la imagen
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
        driver.get(CURP_URL)
        time.sleep(3)

        # 1. Llenar el campo de la CURP
        campo_curp = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.NAME, "strCurp"))
        )
        campo_curp.clear()
        campo_curp.send_keys(payload.curp.upper())

        # 2. Llenar el captcha
        campo_captcha = driver.find_element(By.NAME, "codigo")
        campo_captcha.clear()
        campo_captcha.send_keys(payload.captcha)

        time.sleep(1)

        # 3. Hacer clic en el botón Buscar
        boton = driver.find_element(By.NAME, "sbmBuscar")
        boton.click()

        time.sleep(4)

        # 4. Buscar el enlace del PDF en la página de resultados
        # El botón de descarga suele ser un <a> con href a un PDF o un onclick
        enlaces_pdf = driver.find_elements(By.XPATH, "//a[contains(@href, '.pdf') or contains(@onclick, 'pdf') or contains(@onclick, 'PDF')]")

        if not enlaces_pdf:
            # Buscar botones de "Imprimir" o "Descargar"
            enlaces_pdf = driver.find_elements(By.XPATH, "//input[contains(@value, 'Imprimir') or contains(@value, 'Descargar')] | //button[contains(text(), 'Imprimir') or contains(text(), 'Descargar')]")

        if not enlaces_pdf:
            raise Exception("No se encontró el botón para descargar el PDF en la página de resultados.")

        url_pdf = enlaces_pdf[0].get_attribute('href')

        # Si no tiene href, puede ser un onclick. En ese caso, hacer clic y esperar la descarga.
        if not url_pdf:
            enlaces_pdf[0].click()
            time.sleep(3)
            # Buscar si hay algún iframe o link directo
            raise Exception("El botón no tiene href directo. Se necesita ajustar el código.")

        # 5. Descargar el PDF usando requests con las cookies del navegador
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
