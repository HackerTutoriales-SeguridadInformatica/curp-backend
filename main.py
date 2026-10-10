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

class CurpRequest(BaseModel):
    curp: str
    captcha: str

def crear_driver():
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--window-size=1920,1080')
    return webdriver.Remote(command_executor=SELENIUM_URL, options=options)

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
        driver.get("https://consultas.curp.gob.mx/CurpSP/consulinterCurSP.do")
        time.sleep(2)

        # Llenar la CURP (ajustar el name del input según el HTML real)
        try:
            campo_curp = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.NAME, "curp"))
            )
            campo_curp.clear()
            campo_curp.send_keys(curp.upper())
        except:
            # Intentar con otros nombres comunes
            campo_curp = driver.find_element(By.ID, "curp")
            campo_curp.clear()
            campo_curp.send_keys(curp.upper())

        time.sleep(1)

        # Buscar la imagen del captcha
        img_captcha = driver.find_element(By.XPATH, "//img[contains(@src, 'captcha') or contains(@id, 'captcha') or contains(@name, 'captcha')]")
        img_base64 = img_captcha.screenshot_as_base64

        return {"captcha_img": img_base64}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener captcha: {str(e)}")
    finally:
        if driver:
            driver.quit()

@app.post("/descargar-curp")
async def descargar_curp(payload: CurpRequest):
    """
    Recibe la CURP y el captcha resuelto. Descarga el PDF.
    """
    driver = None
    try:
        driver = crear_driver()
        driver.get("https://consultas.curp.gob.mx/CurpSP/consulinterCurSP.do")
        time.sleep(2)

        # Llenar la CURP
        campo_curp = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.NAME, "curp"))
        )
        campo_curp.clear()
        campo_curp.send_keys(payload.curp.upper())

        # Llenar el captcha
        campo_captcha = driver.find_element(By.NAME, "captcha")
        campo_captcha.clear()
        campo_captcha.send_keys(payload.captcha)

        time.sleep(1)

        # Enviar
        boton = driver.find_element(By.XPATH, "//input[@type='submit'] | //button[@type='submit']")
        boton.click()

        time.sleep(3)

        # Buscar el enlace del PDF
        enlace_pdf = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//a[contains(@href, '.pdf') or contains(@onclick, 'pdf') or contains(text(), 'Imprimir')]"))
        )
        url_pdf = enlace_pdf.get_attribute('href')

        # Descargar con las cookies de la sesión
        session = requests.Session()
        for cookie in driver.get_cookies():
            session.cookies.set(cookie['name'], cookie['value'])

        pdf_response = session.get(url_pdf)
        pdf_response.raise_for_status()

        return Response(content=pdf_response.content, media_type="application/pdf")

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al descargar CURP: {str(e)}")
    finally:
        if driver:
            driver.quit()