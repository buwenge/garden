import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import json
import logging
import os
from pathlib import Path
import tempfile
import time

import httpx

from garden_weather import (
    normalize_observation,
    observation_sort_key,
    parse_timestamp,
    validate_observation,
)

QWEATHER_CITY = ""
QWEATHER_CITY_NAME = ""

CACHE_TTL = 1800  # 30 分钟
_cache: dict = {}
_cache_ts: float = 0
WEATHER_CACHE_SCHEMA_VERSION = 1
WEATHER_CACHE_MAX_AGE = timedelta(hours=72)
WEATHER_CACHE_MAX_OBSERVATIONS = 144


def _get_key():
    return os.getenv("QWEATHER_API_KEY", "")


def _get_host():
    return os.getenv("QWEATHER_HOST", "")


def _get_city():
    return QWEATHER_CITY or os.getenv("QWEATHER_CITY", "")


def weather_cache_path() -> Path:
    """延迟读取环境变量，避免 daemon 的 dotenv 加载顺序把路径锁死。"""
    configured = os.getenv("WEATHER_CACHE_FILE", "").strip()
    return Path(configured) if configured else Path(__file__).parent / "weather_cache.json"


def _cache_lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


@contextmanager
def _cache_locked(path: Path, *, exclusive: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _cache_lock_path(path).open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_cached_observations_unlocked(path: Path, *, now: datetime | None = None) -> list[dict] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        logging.warning("天气事实缓存不可读（cache_invalid）")
        return None
    if not isinstance(data, dict) or data.get("schema_version") != WEATHER_CACHE_SCHEMA_VERSION:
        logging.warning("天气事实缓存格式无效（cache_invalid）")
        return None
    values = data.get("observations")
    if not isinstance(values, list):
        logging.warning("天气事实缓存记录无效（cache_invalid）")
        return None
    observations = [validate_observation(value, now=now) for value in values]
    if any(value is None for value in observations):
        logging.warning("天气事实缓存含非法观测（cache_invalid）")
        return None
    result = [value for value in observations if value is not None]
    if len({value["observation_id"] for value in result}) != len(result):
        logging.warning("天气事实缓存含重复观测（cache_invalid）")
        return None
    return sorted(result, key=observation_sort_key)


def load_weather_observations(
    *,
    location_id: str | None = None,
    path: Path | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """离线读取 daemon 已落盘的有效观测；缓存损坏时宁可返回空。"""
    path = path or weather_cache_path()
    with _cache_locked(path, exclusive=False):
        observations = _read_cached_observations_unlocked(path, now=now)
    if observations is None:
        return []
    if location_id is not None:
        observations = [item for item in observations if item["location_id"] == location_id]
    return observations


def latest_weather_observation(
    *, location_id: str | None = None, path: Path | None = None, now: datetime | None = None,
) -> dict | None:
    observations = load_weather_observations(location_id=location_id, path=path, now=now)
    return observations[-1] if observations else None


def _write_cached_observations_unlocked(observations: list[dict], path: Path) -> None:
    payload = {"schema_version": WEATHER_CACHE_SCHEMA_VERSION, "observations": observations}
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(payload, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def persist_weather_observation(
    observation: dict,
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """在独立锁内去重后原子保存；损坏旧缓存绝不以新文件覆盖。"""
    path = path or weather_cache_path()
    checked = validate_observation(observation, now=now)
    if checked is None:
        logging.warning("天气事实观测被拒绝（observation_invalid）")
        return False
    cutoff_now = now or parse_timestamp(checked["received_at"])
    if cutoff_now is None:
        return False
    if parse_timestamp(checked["observed_at"]) < cutoff_now - WEATHER_CACHE_MAX_AGE:
        return False
    with _cache_locked(path, exclusive=True):
        observations = _read_cached_observations_unlocked(path, now=cutoff_now)
        if observations is None:
            return False
        if any(item["observation_id"] == checked["observation_id"] for item in observations):
            return False
        observations.append(checked)
        cutoff = cutoff_now - WEATHER_CACHE_MAX_AGE
        observations = [
            item for item in observations
            if parse_timestamp(item["observed_at"]) >= cutoff
        ]
        observations.sort(key=observation_sort_key)
        _write_cached_observations_unlocked(observations[-WEATHER_CACHE_MAX_OBSERVATIONS:], path)
    return True


async def fetch_weather() -> dict | None:
    """拉取实时天气 + 3天预报，缓存30分钟"""
    global _cache, _cache_ts

    if _cache and time.time() - _cache_ts < CACHE_TTL:
        return _cache

    key = _get_key()
    host = _get_host()
    if not key or not host:
        logging.warning("天气 API 未配置")
        return None

    base = f"https://{host}"
    city = _get_city()
    params = {"location": city, "key": key}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            now_resp = await client.get(f"{base}/v7/weather/now", params=params)
            forecast_resp = await client.get(f"{base}/v7/weather/3d", params=params)
            if not QWEATHER_CITY_NAME:
                await _resolve_city_name(client, base, city, key)

        now_data = now_resp.json()
        forecast_data = forecast_resp.json()

        if now_data.get("code") != "200":
            logging.warning(f"天气 API now 返回: {now_data.get('code')}")
            return _cache or None

        now = now_data.get("now", {})
        days = forecast_data.get("daily", []) if forecast_data.get("code") == "200" else []

        result = {
            "cityName": QWEATHER_CITY_NAME,
            "temp": now.get("temp"),
            "feelsLike": now.get("feelsLike"),
            "text": now.get("text"),
            "icon": now.get("icon"),
            "humidity": now.get("humidity"),
            "windDir": now.get("windDir"),
            "windScale": now.get("windScale"),
            "precip": now.get("precip"),
            "vis": now.get("vis"),
            "updateTime": now_data.get("updateTime"),
            "forecast": [
                {
                    "date": d.get("fxDate"),
                    "textDay": d.get("textDay"),
                    "textNight": d.get("textNight"),
                    "tempMin": d.get("tempMin"),
                    "tempMax": d.get("tempMax"),
                    "iconDay": d.get("iconDay"),
                }
                for d in days[:3]
            ],
        }

        observation = normalize_observation(
            location_id=city,
            location_name=QWEATHER_CITY_NAME,
            # 顶层 updateTime 只是 API 最近更新时间；now.obsTime 才是
            # 和风定义的真实实况观测时刻，也是去重与降水窗口的锚点。
            observed_time=now.get("obsTime"),
            received_at=datetime.now().astimezone(),
            temp=now.get("temp"),
            feels_like=now.get("feelsLike"),
            humidity=now.get("humidity"),
            wind_scale=now.get("windScale"),
            precip=now.get("precip"),
            condition_text=now.get("text"),
        )
        if observation is None:
            logging.warning("天气实况未写入事实缓存（observation_invalid）")
        else:
            try:
                persist_weather_observation(observation)
            except Exception:
                # 事实缓存是附属层；写盘失败不能反向吞掉本次已成功取得的
                # 实况、预报或原有 Web 天气展示。
                logging.warning("天气实况未写入事实缓存（cache_write_failed）")

        _cache = result
        _cache_ts = time.time()
        return result

    except Exception:
        logging.warning("天气拉取失败（network_error）")
        return _cache or None


async def _resolve_city_name(client: httpx.AsyncClient, base: str, city_id: str, key: str):
    global QWEATHER_CITY_NAME
    try:
        resp = await client.get(f"{base}/geo/v2/city/lookup", params={"location": city_id, "key": key})
        data = resp.json()
        if data.get("code") == "200" and data.get("location"):
            loc = data["location"][0]
            QWEATHER_CITY_NAME = f"{loc.get('adm2', '')} {loc.get('name', '')}".strip()
    except Exception:
        pass


def weather_summary(data: dict | None) -> str:
    """生成给 agent 看的天气摘要"""
    if not data:
        return ""
    city = data.get("cityName", "")
    prefix = f"{city} " if city else ""
    parts = [f"{prefix}{data['temp']}°C {data['text']}"]
    if data.get("humidity"):
        parts.append(f"湿度{data['humidity']}%")
    if data.get("windDir"):
        parts.append(f"{data['windDir']}{data.get('windScale', '')}级")
    forecast = data.get("forecast", [])
    if forecast:
        today = forecast[0]
        parts.append(f"今天{today['tempMin']}~{today['tempMax']}°C")
    return "，".join(parts)


async def search_city(query: str) -> list[dict]:
    """搜索城市，返回 [{id, name, adm1, adm2}]"""
    key = _get_key()
    host = _get_host()
    if not key or not host:
        return []
    base = f"https://{host}"
    params = {"location": query, "key": key, "number": "5"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{base}/geo/v2/city/lookup", params=params)
        data = resp.json()
        if data.get("code") != "200":
            return []
        return [
            {"id": loc["id"], "name": loc["name"], "adm1": loc["adm1"], "adm2": loc["adm2"]}
            for loc in data.get("location", [])
        ]
    except Exception as e:
        logging.warning(f"城市搜索失败: {e}")
        return []
