"""TRON HD 钱包派生模块 — 独立地址架构的核心。

BIP44 路径: m/44'/195'/0'/0/N (195 = TRON coin type, N = 地址序号)

用途: 每订单一个独立收款地址, 替代「共享地址 + 尾数匹配」, 从根上删除付款归属歧义。

安全边界:
  - master mnemonic 是私钥级敏感信息, 只从环境变量 TRON_MASTER_MNEMONIC 读取。
  - 派生出的子私钥可完全控制对应子地址(同一 HD 种子), 归集时用 master 签名子地址转账。
  - 本模块不落盘任何私钥/mnemonic, 只做内存计算。
"""
from __future__ import annotations

import os

from bip_utils import (
    Bip39SeedGenerator,
    Bip44,
    Bip44Changes,
    Bip44Coins,
)
from tronpy.keys import PrivateKey


def load_mnemonic() -> str:
    """读取 master mnemonic(私钥级, 不落盘)。优先环境变量, 回退项目 .env 文件。"""
    m = os.environ.get("TRON_MASTER_MNEMONIC", "").strip()
    if m:
        return m
    # fallback: 读项目 .env 文件(systemd 走 EnvironmentFile, 手动跑时直接读文件)
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        import re
        with open(env_path) as f:
            for line in f:
                mm = re.search(r'TRON_MASTER_MNEMONIC\s*=\s*["\']?(.+?)["\']?\s*$', line)
                if mm:
                    return mm.group(1).strip()
    raise RuntimeError("TRON_MASTER_MNEMONIC 未配置")


def derive_private_key(mnemonic: str, index: int) -> bytes:
    """派生 m/44'/195'/0'/0/{index} 的 32 字节 raw 私钥。"""
    if index < 0:
        raise ValueError("index 必须 >= 0")
    seed = Bip39SeedGenerator(mnemonic).Generate()
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
