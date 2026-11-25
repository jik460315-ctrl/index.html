import requests
import os
import uuid
import subprocess
from pathlib import Path
 
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI(title="CLIPVEX AI Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path("storage")
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "clips"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

@app.get("/")
def home():
    return {
        "message": "CLIPVEX AI Backend is running!",
        "status": "online"
    }

@app.get("/health")
def health():
    return {"status": "healthy"}
@app.post("/analyze/{video_id}")
def analyze_video(video_id: str):
    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="Gemini API key is missing"
        )

    matches = list(UPLOAD_DIR.glob(video_id + ".*"))

    if not matches:
        raise HTTPException(
            status_code=404,
            detail="Video not found"
        )

    source = matches[0]

    try:
        # Upload video to Gemini
        start_upload = requests.post(
            "https://generativelanguage.googleapis.com/upload/v1beta/files",
            params={"key": api_key},
            headers={
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(source.stat().st_size),
                "X-Goog-Upload-Header-Content-Type": "video/mp4",
                "Content-Type": "application/json"
            },
            json={"file": {"display_name": source.name}},
            timeout=60
        )

        start_upload.raise_for_status()
        upload_url = start_upload.headers.get("X-Goog-Upload-URL")

        if not upload_url:
            raise HTTPException(
                status_code=500,
                detail="Gemini upload URL missing"
            )

        with open(source, "rb") as video_file:
            uploaded = requests.post(
                upload_url,
                headers={
                    "X-Goog-Upload-Offset": "0",
                    "X-Goog-Upload-Command": "upload, finalize"
                },
                data=video_file,
                timeout=600
            )

        uploaded.raise_for_status()
        gemini_file = uploaded.json()["file"]

        # Wait until Gemini finishes processing the video
        file_name = gemini_file["name"]

        for _ in range(30):
            file_status = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/{file_name}",
                params={"key": api_key},
                timeout=30
            )

            file_status.raise_for_status()
            gemini_file = file_status.json()

            if gemini_file.get("state") == "ACTIVE":
                break

            if gemini_file.get("state") == "FAILED":
                raise HTTPException(
                    status_code=500,
                    detail="Gemini video processing failed"
                )

            import time
            time.sleep(5)

        if gemini_file.get("state") != "ACTIVE":
            raise HTTPException(
                status_code=504,
                detail="Gemini video processing timed out"
            )

        # Ask AI to find engaging moments
        prompt = """
Analyze this video and find 5 potentially engaging short-form moments.

Look for funny, emotional, surprising, exciting, useful, or attention-grabbing moments.
Choose moments that make sense as standalone clips.
Do not invent events.

Return ONLY valid JSON in this format:
{
  "moments": [
    {
      "start": 12.5,
      "end": 45.0,
      "reason": "Why this moment is engaging",
      "score": 85
    }
  ]
}

Choose moments approximately 20 to 60 seconds long.
Use timestamps in seconds.
"""

        result = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            params={"key": api_key},
            json={
                "contents": [{
                    "parts": [
                        {"file_data": {
                            "mime_type": gemini_file["mimeType"],
                            "file_uri": gemini_file["uri"]
                        }},
                        {"text": prompt}
                    ]
                }],
                "generationConfig": {
                    "temperature": 0.3,
                    "responseMimeType": "application/json"
                }
            },
            timeout=300
        )

        result.raise_for_status()
        data = result.json()
        ai_text = data["candidates"][0]["content"]["parts"][0]["text"]

        return {
            "video_id": video_id,
            "moments": __import__("json").loads(ai_text),
            "message": "AI analysis completed"
        }

    except HTTPException:
        raise

    except requests.RequestException as error:
    raise HTTPException(
        status_code=502,
        detail=f"Gemini API error: {str(error)}"
    )

    except Exception:
        raise HTTPException(
            status_code=500,
            detail="AI analysis failed"
        )
@app.post("/upload")
async def upload_video(file: UploadFile = File(...)):
    allowed = {
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "video/x-matroska"
    }

    if file.content_type not in allowed:
        raise HTTPException(
            status_code=400,
            detail="Unsupported video format"
        )

    video_id = str(uuid.uuid4())
    extension = Path(file.filename or "video.mp4").suffix.lower()

    if extension not in [".mp4", ".mov", ".webm", ".mkv"]:
        extension = ".mp4"

    file_path = UPLOAD_DIR / f"{video_id}{extension}"

    size = 0
    max_size = 500 * 1024 * 1024

    try:
        with open(file_path, "wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break

                size += len(chunk)

                if size > max_size:
                    raise HTTPException(
                        status_code=413,
                        detail="Video exceeds 500 MB limit"
                    )

                output.write(chunk)

    except Exception:
        file_path.unlink(missing_ok=True)
        raise

    finally:
        await file.close()

    return {
        "video_id": video_id,
        "filename": file_path.name,
        "size": size,
        "message": "Video uploaded successfully"
    }


@app.post("/clip/{video_id}")
def create_clip(
    video_id: str,
    start: float = 0,
    end: float = 30
):
    if start < 0 or end <= start or end - start > 180:
        raise HTTPException(
            status_code=400,
            detail="Invalid clip duration"
        )

    matches = list(UPLOAD_DIR.glob(video_id + ".*"))

    if not matches:
        raise HTTPException(
            status_code=404,
            detail="Video not found"
        )

    source = matches[0]
    output = OUTPUT_DIR / f"{uuid.uuid4()}.mp4"

    command = [
        "ffmpeg",
        "-y",
        "-ss", str(start),
        "-i", str(source),
        "-t", str(end - start),
        "-c:v", "libx264",
        "-c:a", "aac",
        "-movflags", "+faststart",
        str(output)
    ]

    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=600
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=503,
            detail="FFmpeg is not installed on the server"
        )
    except subprocess.CalledProcessError:
        raise HTTPException(
            status_code=500,
            detail="Video processing failed"
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="Video processing timed out"
        )

    return {
        "clip_id": output.stem,
        "download_url": f"/download/{output.name}",
        "message": "MP4 clip created successfully"
    }


@app.get("/download/{filename}")
def download_clip(filename: str):
    if Path(filename).name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = OUTPUT_DIR / filename

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Clip not found")

    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename=filename
    )
