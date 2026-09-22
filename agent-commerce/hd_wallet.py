"""TRON HD 钱包派生模块 — 独立地址架构的核心。

BIP44 路径: m/44'/195'/0'/0/N (195 = TRON coin type, N = 地址序号)

用途: 每订单一个独立收款地址, 替代「共享地址 + 尾数匹配」, 从根上删除付款归属歧义。

安全边界:
  - master mnemonic 是私钥级敏感信息, 只从环境变量 TRON_MASTER_MNEMONIC 读取。
  - 派生出的子私钥可完全控制对应子地址(同一 HD 种子), 归集时用 master 签名子地址转账。
  - 本模块不落盘任何私钥/mnemonic, 只做内存计算(seed 缓存除外)。
"""
from __future__ import annotations

import os
import re

from bip_utils import (
    Bip39SeedGenerator,
    Bip44,
    Bip44Changes,
    Bip44Coins,
)
from tronpy.keys import PrivateKey

MAX_INDEX = 2 ** 31  # BIP44 硬化边界, 越界说明派生分支错误


def _env_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_mnemonic() -> str:
    """读取 master mnemonic(私钥级, 不落盘)。优先环境变量, 回退项目 .env 文件。"""
    m = os.environ.get("TRON_MASTER_MNEMONIC", "").strip()
    if m:
        return m
    # fallback: 读项目 .env 文件(systemd 走 EnvironmentFile, 手动跑时直接读文件)
    env_path = _env_path()
    if os.path.exists(env_path):
        st = os.stat(env_path)
        if st.st_mode & 0o077:
            raise RuntimeError(
                f"{env_path} 权限过宽({oct(st.st_mode & 0o777)}), 拒绝读取(应为 600)"
            )
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue  # 跳过注释/空行, 防止误匹配注释里的旧助记词
                mm = re.match(
                    r'^(?:export\s+)?TRON_MASTER_MNEMONIC\s*=\s*["\']?(.+?)["\']?\s*$',
                    line,
                )
                if mm:
                    return mm.group(1).strip()
    raise RuntimeError("TRON_MASTER_MNEMONIC 未配置")


_seed_cache: tuple[str, bytes] | None = None


def _seed(mnemonic: str) -> bytes:
    """PBKDF2 生成 seed, 按 mnemonic 缓存(开单在写事务内调用, 避免重复 2048 轮计算)。"""
    global _seed_cache
    if _seed_cache is None or _seed_cache[0] != mnemonic:
        _seed_cache = (mnemonic, Bip39SeedGenerator(mnemonic).Generate())
    return _seed_cache[1]


def derive_private_key(mnemonic: str, index: int) -> bytes:
    """派生 m/44'/195'/0'/0/{index} 的 32 字节 raw 私钥。"""
    if index < 0 or index >= MAX_INDEX:
        raise ValueError(f"index 必须在 [0, {MAX_INDEX}) 范围, 收到 {index}")
    seed = _seed(mnemonic)
    bip44_mst = Bip44.FromSeed(seed, Bip44Coins.TRON)
    acc = (
        bip44_mst.Purpose()
        .Coin()
        .Account(0)
        .Change(Bip44Changes.CHAIN_EXT)
        .AddressIndex(index)
    )
    return acc.PrivateKey().Raw().ToBytes()


def derive_address(mnemonic: str, index: int) -> str:
    """派生第 index 个子地址的 base58check 地址(以 T 开头)。"""
    return PrivateKey(derive_private_key(mnemonic, index)).public_key.to_base58check_address()


def verify_wallet(receive_address: str) -> str:
    """启动自检: index 0 派生地址必须 == RECEIVE_ADDRESS, 否则拒绝启动。

    防止助记词配置错误导致收款到不可控地址(收到没有私钥的地址上)。
    返回 index 0 地址。
    """
    mnemonic = load_mnemonic()
    a0 = derive_address(mnemonic, 0)
    if a0 != receive_address:
        raise SystemExit(
            f"FATAL: HD index0 派生地址 {a0} != RECEIVE_ADDRESS {receive_address}。"
            f"助记词错误或 RECEIVE_ADDRESS 配置错误, 拒绝启动(防止收款到不可控地址)"
        )
    return a0
