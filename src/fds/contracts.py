import base64
import binascii
import io
import json
from typing import Annotated, Literal

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Choice(StrictModel):
    type: Literal["choice"]
    instructions: str = Field(default="", max_length=3000)
    criteria: dict[str, str] = Field(min_length=2, max_length=64)

    @model_validator(mode="after")
    def valid(self):
        if any(not k or len(k) > 100 or k.isdecimal() or len(v) > 1000 for k, v in self.criteria.items()):
            raise ValueError("選択肢は短い文字列IDと1000文字以内の説明にしてください。数字だけのIDは使えません。")
        return self


class Noul(StrictModel):
    type: Literal["noul"]
    instructions: str = Field(default="", max_length=3000)
    criteria: dict[Literal["true", "false"], str] | None = None

    @model_validator(mode="after")
    def valid(self):
        if self.criteria is not None and (set(self.criteria) != {"true", "false"} or any(len(v) > 1000 for v in self.criteria.values())):
            raise ValueError("真偽の説明はtrue/falseの両方を指定してください。")
        return self


class Score(StrictModel):
    type: Literal["score"]
    instructions: str = Field(default="", max_length=3000)
    criteria: list[str] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def valid(self):
        if any(len(v) > 1000 for v in self.criteria):
            raise ValueError("尺度の説明が長すぎます。")
        return self


class ModelOperation(StrictModel):
    model: str = Field(min_length=1, max_length=100)
    device: Literal["auto", "cpu", "cuda"] = "auto"
    auto_unload: bool = Field(default=True, strict=True)
    approval_token: str | None = Field(default=None, min_length=20, max_length=256, repr=False)
    timeout_seconds: int = Field(default=30, ge=1, le=300, strict=True)


class DecisionRequest(StrictModel):
    model: str = Field(default="jeff-qwen-2b", min_length=1, max_length=100)
    device: Literal["auto", "cpu", "cuda"] = "auto"
    auto_unload: bool = Field(default=True, strict=True)
    approval_token: str | None = Field(default=None, min_length=20, max_length=256, repr=False)
    state: str = Field(max_length=24000)
    questions: dict[str, Annotated[Choice | Noul | Score, Field(discriminator="type")]] = Field(min_length=1, max_length=8)
    images: list[Annotated[str, Field(max_length=10_666_700)]] = Field(default_factory=list, max_length=4)
    timeout_seconds: int = Field(default=30, ge=1, le=300, strict=True)
    priority: Literal["interactive", "normal", "background"] = "normal"

    @model_validator(mode="after")
    def bounded(self):
        if any(not k or len(k) > 100 for k in self.questions):
            raise ValueError("質問IDが不正です。")
        if len(json.dumps(self.model_dump(exclude={"images"}), ensure_ascii=False).encode()) > 65536:
            raise ValueError("テキスト入力は64KiB以内にしてください。")
        return self


def prepare_images(values: list[str]) -> list[Image.Image]:
    result = []
    total = 0
    for value in values:
        head, sep, encoded = value.partition(",")
        if not sep or head not in {"data:image/png;base64", "data:image/jpeg;base64", "data:image/webp;base64"}:
            raise ValueError("画像はbase64形式のPNG/JPEG/WebPを指定してください。URLとファイルパスは受け付けません。")
        try:
            if len(encoded) > 10_666_668:
                raise ValueError("画像は1枚8MB以内にしてください。")
            raw = base64.b64decode(encoded, validate=True)
            total += len(raw)
            if len(raw) > 8_000_000 or total > 16_000_000:
                raise ValueError("画像は1枚8MB、合計16MB以内にしてください。")
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 16_000_000:
                    raise ValueError("画像の形式または画素数が上限外です。")
                expected = {"data:image/png;base64": "PNG", "data:image/jpeg;base64": "JPEG", "data:image/webp;base64": "WEBP"}[head]
                if image.format != expected:
                    raise ValueError("画像形式とdata URLの種類が一致しません。")
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                normalized = ImageOps.exif_transpose(image).convert("RGBA")
                normalized.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
                clean = Image.new("RGB", normalized.size, "white")
                clean.paste(normalized, mask=normalized.getchannel("A"))
                result.append(clean)
        except (binascii.Error, OSError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError) as error:
            raise ValueError("画像データを読み込めません。") from error
    return result
