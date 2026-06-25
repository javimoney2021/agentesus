import asyncio
import uuid
from pathlib import Path

import boto3

from core.config import (
    R2_ACCESS_KEY,
    R2_BUCKET,
    R2_ENDPOINT,
    R2_PUBLIC_URL,
    R2_SECRET_KEY,
)


r2_client = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto",
)


async def upload_to_r2(file_bytes: bytes, filename: str) -> str:
    ext = Path(filename).suffix
    unique_key = f"adjuntos/{uuid.uuid4().hex}{ext}"

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: r2_client.put_object(
            Bucket=R2_BUCKET,
            Key=unique_key,
            Body=file_bytes,
        )
    )
    return f"{R2_PUBLIC_URL}/{unique_key}"


async def delete_from_r2(url: str):
    try:
        key = url.replace(f"{R2_PUBLIC_URL}/", "")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: r2_client.delete_object(Bucket=R2_BUCKET, Key=key)
        )
    except Exception as e:
        print(f"⚠️ No se pudo borrar objeto de R2: {e}")
