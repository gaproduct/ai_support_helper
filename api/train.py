"""Article chunking + KB upload endpoints (used by /train UI)."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import train

router = APIRouter()


class ChunkRequest(BaseModel):
    text: str


class UploadRequest(BaseModel):
    chunks: list[dict]


@router.post("/api/chunk")
def api_chunk(body: ChunkRequest) -> dict:
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="Текст не может быть пустым")
    return {"chunks": train.chunk_text(body.text)}


@router.post("/api/upload-chunks")
def api_upload_chunks(body: UploadRequest) -> dict:
    if not body.chunks:
        raise HTTPException(status_code=400, detail="Нет чанков для загрузки")
    uploaded, errors = train.upload_chunks(body.chunks)
    return {"uploaded": uploaded, "errors": errors}
