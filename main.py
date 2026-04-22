import os
import json
import base64
import logging
import sys
import pathlib
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from google.cloud import vision

load_dotenv()

LOGGER_NAME = "captionfast_proxy"
logger = logging.getLogger(LOGGER_NAME)


def _runtime_base_dir() -> pathlib.Path:
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path.cwd()


def _configure_logging() -> None:
    if logger.handlers:
        return

    logs_dir = _runtime_base_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "proxy.log"
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False


_configure_logging()
client: vision.ImageAnnotatorClient | None = None


def _configure_google_credentials() -> None:
    gcp_json_key = os.getenv("GCP_JSON_KEY")
    if gcp_json_key:
        try:
            decoded_key = base64.b64decode(gcp_json_key).decode("utf-8")
            parsed_credentials = json.loads(decoded_key)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            try:
                parsed_credentials = json.loads(gcp_json_key)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Invalid GCP_JSON_KEY: not valid JSON") from exc

        temp_dir = pathlib.Path(
            os.getenv("TMPDIR") or os.getenv("TEMP") or "/tmp"
        )
        temp_credentials_path = temp_dir / "google-credentials.json"
        temp_credentials_path.write_text(
            json.dumps(parsed_credentials), encoding="utf-8"
        )
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(temp_credentials_path)
        logger.info("Configured Google credentials from GCP_JSON_KEY.")
        return

    local_credentials_path = _runtime_base_dir() / "google-credentials.json"
    if local_credentials_path.exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(local_credentials_path)
        logger.info("Configured Google credentials from local file.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global client
    _configure_google_credentials()
    client = vision.ImageAnnotatorClient()
    logger.info("Proxy starting on %s", int(os.environ.get("PORT", 8002)))
    yield


app = FastAPI(title="CaptionFast Local Backend", lifespan=lifespan)


def _validate_api_key(x_api_key: str | None) -> None:
    expected_api_key = os.getenv("PROXY_API_KEY")

    if not expected_api_key:
        logger.info("API key check: fail (expected API key missing)")
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: PROXY_API_KEY is not set.",
        )

    if x_api_key != expected_api_key:
        logger.info("API key check: fail (incoming key mismatch)")
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info("API key check: pass")


@app.post("/api/v1/scan")
async def scan_image(
    file: UploadFile = File(...),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, str]:
    _validate_api_key(x_api_key)

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Uploaded file must be an image.")

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Uploaded image is empty.")

    logger.info(
        "/api/v1/scan received: filename=%s, content_type=%s, file_size_bytes=%s",
        file.filename,
        file.content_type,
        len(image_bytes),
    )

    try:
        logger.info("Sending to Google Vision")
        image = vision.Image(content=image_bytes)
        if client is None:
            raise HTTPException(status_code=500, detail="Vision client is not initialized.")
        vision_response = client.document_text_detection(image=image)
    except Exception as exc:
        logger.exception("Exception while calling Vision API")
        raise HTTPException(status_code=502, detail=f"Vision request failed: {exc}") from exc

    if vision_response.error.message:
        raise HTTPException(
            status_code=500,
            detail=f"Vision API Error: {vision_response.error.message}",
        )

    annotations = vision_response.text_annotations
    if not annotations:
        return {"status": "success", "text": ""}

    extracted_text = annotations[0].description.strip()
    logger.info("Vision response extracted text: %s", extracted_text)
    return {"status": "success", "text": extracted_text}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8002)),
    )
