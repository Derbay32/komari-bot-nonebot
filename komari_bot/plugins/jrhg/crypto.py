"""
基于机器指纹的配置加密工具。

使用 Fernet (AES-128) 对敏感字段进行加密。
"""
import base64
import hashlib
import platform
from typing import Optional

from cryptography.fernet import Fernet


def _get_machine_key() -> bytes:
    """生成基于机器指纹的加密密钥。

    Returns:
        URL 安全的 Base64 编码密钥
    """
    # 组合机器特征
    machine_id = f"{platform.node()}-{platform.machine()}-{platform.system()}"
    hash_key = hashlib.sha256(machine_id.encode()).digest()
    # Fernet 需要 32 字节 Base64 编码密钥
    return base64.urlsafe_b64encode(hash_key[:32])


# 全局 Fernet 实例
_fernet: Optional[Fernet] = None


def get_fernet() -> Fernet:
    """获取 Fernet 加密器实例（单例）。

    Returns:
        Fernet 加密器实例
    """
    global _fernet
    if _fernet is None:
        key = _get_machine_key()
        _fernet = Fernet(key)
    return _fernet


def encrypt_token(token: str) -> str:
    """加密 API token。

    Args:
        token: 原始 token

    Returns:
        加密后的 token（Base64 编码）
    """
    if not token:
        return ""
    fernet = get_fernet()
    encrypted = fernet.encrypt(token.encode())
    return encrypted.decode()


def decrypt_token(encrypted_token: str) -> str:
    """解密 API token。

    Args:
        encrypted_token: 加密的 token

    Returns:
        原始 token

    Raises:
        ValueError: 如果解密失败
    """
    if not encrypted_token:
        return ""
    try:
        fernet = get_fernet()
        decrypted = fernet.decrypt(encrypted_token.encode())
        return decrypted.decode()
    except Exception as e:
        raise ValueError(f"Token 解密失败: {e}") from e
