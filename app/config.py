"""全局配置：路径、DeepSeek、向量模型等。

DeepSeek Key 的读取优先级：
1. 进程环境变量（DEEPSEEK_API_KEY / deepseek_api_key 等，大小写不敏感）
2. Windows 注册表中的系统/用户环境变量（解决“设置了变量但当前进程没继承”的情况）
3. 项目根目录 .env 文件
值为空或仍是占位符时视为未配置，进入演示模式。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "kb.sqlite3"
SAMPLE_DIR = BASE_DIR / "示例知识库"

# 进程启动时的环境变量快照：系统环境变量优先于 .env
_OS_ENV = dict(os.environ)


def _read_env_file() -> dict[str, str]:
    path = BASE_DIR / ".env"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


ENV_FILE = _read_env_file()


def _from_process(name: str) -> str:
    for key, value in _OS_ENV.items():
        if key.lower() == name.lower() and value:
            return value
    return ""


def _from_registry(name: str) -> str:
    """Windows 下直接读注册表，避免变量设置后旧进程读不到。"""
    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    locations = (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    )
    for root, path in locations:
        try:
            with winreg.OpenKey(root, path) as key:
                index = 0
                while True:
                    try:
                        value_name, value_data, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    if value_name.lower() == name.lower() and str(value_data).strip():
                        return str(value_data)
        except OSError:
            continue
    return ""


def _from_file(name: str) -> str:
    for key, value in ENV_FILE.items():
        if key.lower() == name.lower() and value:
            return value
    return ""


def env_value(name: str, default: str = "") -> str:
    for getter in (_from_process, _from_registry, _from_file):
        value = getter(name)
        if value and value.strip():
            return value.strip().strip('"').strip("'")
    return default


def _is_placeholder(value: str) -> bool:
    low = (value or "").strip().lower()
    if not low:
        return True
    return "xxxx" in low or low in {"sk-xxx", "your-key", "your_api_key", "none", "null"}


def _read_key(*names: str) -> tuple[str, str]:
    """返回 (key, 来源)。环境变量 > 注册表 > .env。"""
    for name in names:
        for source, getter in (("system_env", _from_process),
                               ("system_registry", _from_registry),
                               (".env", _from_file)):
            value = getter(name)
            if value and not _is_placeholder(value):
                return value.strip().strip('"').strip("'"), source
    return "", "none"


DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---- 大模型：DeepSeek（OpenAI 兼容接口）----
DEEPSEEK_API_KEY, DEEPSEEK_KEY_SOURCE = _read_key(
    "DEEPSEEK_API_KEY", "DEEPSEEK_APIKEY", "DEEPSEEK_KEY",
    "deepseek_api_key", "deepseep_api_key",
)
DEEPSEEK_BASE_URL = env_value("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
DEEPSEEK_MODEL = env_value("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_TEMPERATURE = float(env_value("DEEPSEEK_TEMPERATURE", "0.2"))

# ---- 向量检索：local（本地字符语义，默认）/ none / dashscope / openai_compatible ----
EMBEDDING_PROVIDER = env_value("EMBEDDING_PROVIDER", "local").lower()
EMBEDDING_API_KEY = env_value("EMBEDDING_API_KEY") or env_value("DASHSCOPE_API_KEY")
EMBEDDING_BASE_URL = env_value("EMBEDDING_BASE_URL").rstrip("/")
EMBEDDING_MODEL = env_value("EMBEDDING_MODEL", "text-embedding-v4")

if EMBEDDING_PROVIDER == "dashscope":
    EMBEDDING_BASE_URL = EMBEDDING_BASE_URL or "https://dashscope.aliyuncs.com/compatible-mode/v1"

# “local” 使用内置字符向量，不需要任何 Key；其余 provider 仍走外部 Embedding API
LOCAL_SEMANTIC = EMBEDDING_PROVIDER in ("local", "char", "char_ngram", "builtin")

# ---- 切分参数 ----
CHUNK_SIZE = int(env_value("CHUNK_SIZE", "520"))
CHUNK_OVERLAP = int(env_value("CHUNK_OVERLAP", "80"))
TOP_K = int(env_value("TOP_K", "5"))

MAX_UPLOAD_MB = int(env_value("MAX_UPLOAD_MB", "30"))
ALLOWED_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".txt", ".md"}
