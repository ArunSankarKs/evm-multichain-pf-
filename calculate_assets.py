#!/usr/bin/env python3
"""Calculate the USD value of assets for EVM addresses across chains.

Put wallets in ADDRESSES below, or in addresses.txt (one per line).
You can also pass them on the command line.

Examples:
  python calculate_assets.py
  python calculate_assets.py --file addresses.txt
  python calculate_assets.py 0xabc... 0xdef...
  python calculate_assets.py 0xabc...,0xdef... --out report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import httpx
from dotenv import load_dotenv

# Wallets to value. CLI arguments and addresses.txt are merged with this list.
ADDRESSES: list[str] = [
    # "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
]

DEFAULT_ADDRESS_FILE = "addresses.txt"
DEFAULT_JSON_OUT = "portfolio.json"
GOLDRUSH_BASE = "https://api.covalenthq.com/v1"

WALLET_CONCURRENCY = 6
CHAIN_CONCURRENCY = 20
UNIT_REQUESTS_PER_SEC = 5.0

HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
HYPERUNIT_API = "https://api.hyperunit.xyz"
HYPEREVM_RPC = "https://rpc.hyperliquid.xyz/evm"
ETH_RPC = "https://ethereum-rpc.publicnode.com"
HL_STABLES = {"USDC", "USDH", "USDT", "USD₮", "USD₮0", "USDE", "USD0"}
# Unit (HIP-1) tickers are not in allMids; price them as the native asset.
UNIT_UNDERLYING = {
    "UETH": "ETH",
    "UBTC": "BTC",
    "USOL": "SOL",
    "UPUMP": "PUMP",
    "UFART": "FARTCOIN",
    "UUUSPX": "SPX",
    "UBONK": "BONK",
    "UZEC": "ZEC",
    "UAVAX": "AVAX",
    "UVIRT": "VIRTUAL",
    "UANSEM": "ANSEM",
    "XPL": "XPL",
}
UNIT_ASSET_TICKER = {
    "eth": "UETH",
    "btc": "UBTC",
    "sol": "USOL",
    "pump": "UPUMP",
    "fart": "UFART",
    "spxs": "UUUSPX",
    "spx": "UUUSPX",
    "bonk": "UBONK",
    "zec": "UZEC",
    "avax": "UAVAX",
    "virtual": "UVIRT",
    "ansem": "UANSEM",
    "xpl": "XPL",
    "mon": "UMON",
}
UNIT_ASSET_DECIMALS = {
    "eth": 18,
    "btc": 8,
    "sol": 9,
    "pump": 6,
    "fart": 6,
    "spxs": 8,
    "spx": 8,
    "bonk": 5,
    "zec": 8,
    "avax": 18,
    "virtual": 18,
    "ansem": 6,
    "xpl": 18,
    "mon": 18,
    "2z": 9,
}
UNIT_PENDING_STATES = {
    "sourceTxDiscovered",
    "waitForSrcTxFinalization",
    "buildingDstTx",
    "signTx",
    "broadcastTx",
    "waitForDstTxFinalization",
}
BALANCE_OF_SELECTOR = "0x70a08231"
DECIMALS_SELECTOR = "0x313ce567"
LLAMA_PRICES = "https://coins.llama.fi/prices/current"
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
DOMAIN_RE = re.compile(r"^[a-zA-Z0-9.-]+\.(eth|lens|crypto|nft|wallet|dao)$", re.I)
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

EVM_MAINNETS: tuple[str, ...] = (
    "eth-mainnet",
    "matic-mainnet",
    "bsc-mainnet",
    # "gnosis-mainnet",
    "optimism-mainnet",
    "base-mainnet",
    "arbitrum-mainnet",
    "arbitrum-nova-mainnet",
    "avalanche-mainnet",
    "linea-mainnet",
    "scroll-mainnet",
    "zksync-mainnet",
    "blast-mainnet",
    # "mantle-mainnet",
    # "taiko-mainnet",
    # "unichain-mainnet",
    # "world-mainnet",
    # "sonic-mainnet",
    # "sei-mainnet",
    # "berachain-mainnet",
    "hyperevm-mainnet",
    # "ink-mainnet",
    # "apechain-mainnet",
    # "plasma-mainnet",
    # "celo-mainnet",
    "fantom-mainnet",
    # "moonbeam-mainnet",
    # "moonbeam-moonriver",
    "cronos-mainnet",
    "cronos-zkevm-mainnet",
    "bnb-opbnb-mainnet",
    # "zetachain-mainnet",
    # "redstone-mainnet",
    # "canto-mainnet",
    # "viction-mainnet",
    # "adi-mainnet",
    # "axie-mainnet",
    # "oasis-sapphire-mainnet",
    # "emerald-paratime-mainnet",
    "megaeth-mainnet",
    "monad-mainnet",
)

FOUNDATIONAL_CHAINS: tuple[str, ...] = (
    "eth-mainnet",
    "matic-mainnet",
    "bsc-mainnet",
    "gnosis-mainnet",
    "optimism-mainnet",
    "base-mainnet",
)

CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "INR": "₹",
    "JPY": "¥",
    "CNY": "¥",
    "KRW": "₩",
    "CAD": "CA$",
    "AUD": "A$",
    "CHF": "CHF ",
    "SGD": "S$",
}

NON_EVM_PREFIXES = ("btc-", "solana-", "hypercore-")


@dataclass(frozen=True)
class ExplorerChain:
    slug: str
    name: str
    chain_id: int
    explorer: str
    native_symbol: str
    llama: str


BLOCKSCOUT_CHAINS: tuple[ExplorerChain, ...] = (
    ExplorerChain("ethereum", "Ethereum", 1, "https://eth.blockscout.com", "ETH", "ethereum"),
    ExplorerChain("base", "Base", 8453, "https://base.blockscout.com", "ETH", "base"),
    ExplorerChain("optimism", "Optimism", 10, "https://optimism.blockscout.com", "ETH", "optimism"),
    ExplorerChain("arbitrum", "Arbitrum One", 42161, "https://arbitrum.blockscout.com", "ETH", "arbitrum"),
    ExplorerChain("polygon", "Polygon", 137, "https://polygon.blockscout.com", "POL", "polygon"),
    # ExplorerChain("gnosis", "Gnosis", 100, "https://gnosis.blockscout.com", "xDAI", "xdai"),
    # ExplorerChain("scroll", "Scroll", 534352, "https://scroll.blockscout.com", "ETH", "scroll"),
    # ExplorerChain("zksync", "zkSync Era", 324, "https://zksync.blockscout.com", "ETH", "era"),
    # ExplorerChain("celo", "Celo", 42220, "https://explorer.celo.org", "CELO", "celo"),
    # ExplorerChain("unichain", "Unichain", 130, "https://unichain.blockscout.com", "ETH", "unichain"),
    # ExplorerChain("ink", "Ink", 57073, "https://explorer.inkonchain.com", "ETH", "ink"),
    # ExplorerChain("worldchain","World Chain",480,"https://worldchain-mainnet.explorer.alchemy.com","ETH","wc",),
)

FAST_EXPLORER_SLUGS = {"ethereum", "polygon", "base", "optimism", "arbitrum", "gnosis"}

RPC_CHAINS: tuple[ExplorerChain, ...] = (
    ExplorerChain("bsc", "BNB Smart Chain", 56, "https://bsc-dataseed.binance.org", "BNB", "bsc"),
    ExplorerChain("avalanche", "Avalanche C-Chain", 43114, "https://api.avax.network/ext/bc/C/rpc", "AVAX", "avax"),
    ExplorerChain("polygon_zkevm", "Polygon zkEVM", 1101, "https://zkevm-rpc.com", "ETH", "polygon_zkevm"),
)

# Native RPC fallbacks when a Blockscout v2 explorer returns 5xx.
RPC_FALLBACKS = {
    "polygon": ExplorerChain(
        "polygon", "Polygon", 137, "https://polygon-bor-rpc.publicnode.com", "POL", "polygon"
    ),
    "base": ExplorerChain(
        "base", "Base", 8453, "https://base-rpc.publicnode.com", "ETH", "base"
    ),
    "ethereum": ExplorerChain(
        "ethereum", "Ethereum", 1, "https://ethereum-rpc.publicnode.com", "ETH", "ethereum"
    ),
    "optimism": ExplorerChain(
        "optimism", "Optimism", 10, "https://optimism-rpc.publicnode.com", "ETH", "optimism"
    ),
    "arbitrum": ExplorerChain(
        "arbitrum",
        "Arbitrum One",
        42161,
        "https://arbitrum-one-rpc.publicnode.com",
        "ETH",
        "arbitrum",
    ),
    "gnosis": ExplorerChain(
        "gnosis", "Gnosis", 100, "https://gnosis-rpc.publicnode.com", "xDAI", "xdai"
    ),
}

ALL_EXPLORER_CHAINS = BLOCKSCOUT_CHAINS + RPC_CHAINS
LLAMA_BY_SLUG = {chain.slug: chain.llama for chain in ALL_EXPLORER_CHAINS}
RPC_SLUGS = {chain.slug for chain in RPC_CHAINS}

EXPLORER_ALIASES = {
    "matic": "polygon",
    "matic-mainnet": "polygon",
    "polygon-pos": "polygon",
    "polygon-mainnet": "polygon",
    "polygon_pos": "polygon",
    "pol": "polygon",
    "zkevm": "polygon_zkevm",
    "polygon-zkevm": "polygon_zkevm",
    "polygon_zkevm": "polygon_zkevm",
    "eth": "ethereum",
    "eth-mainnet": "ethereum",
    "ethereum-mainnet": "ethereum",
    "arb": "arbitrum",
    "arbitrum-one": "arbitrum",
    "arbitrum-mainnet": "arbitrum",
    "op": "optimism",
    "optimism-mainnet": "optimism",
    "base-mainnet": "base",
    "bsc-mainnet": "bsc",
    "bnb": "bsc",
    "avax": "avalanche",
    "avalanche-mainnet": "avalanche",
    "gnosis-mainnet": "gnosis",
    "xdai": "gnosis",
    "hyperliquid": "hyperliquid",
    "hl": "hyperliquid",
    "hype": "hyperliquid",
    "hyperliquid-dex": "hyperliquid",
}

GOLDRUSH_ALIASES = {
    "polygon": "matic-mainnet",
    "matic": "matic-mainnet",
    "polygon-pos": "matic-mainnet",
    "polygon-mainnet": "matic-mainnet",
    "ethereum": "eth-mainnet",
    "eth": "eth-mainnet",
    "base": "base-mainnet",
    "optimism": "optimism-mainnet",
    "op": "optimism-mainnet",
    "arbitrum": "arbitrum-mainnet",
    "arb": "arbitrum-mainnet",
    "bsc": "bsc-mainnet",
    "avalanche": "avalanche-mainnet",
    "gnosis": "gnosis-mainnet",
    "polygon_zkevm": "polygon-zkevm-mainnet",
    "polygon-zkevm": "polygon-zkevm-mainnet",
    "zkevm": "polygon-zkevm-mainnet",
}


def normalize_explorer_slug(raw: str) -> str:
    return EXPLORER_ALIASES.get(raw.strip().lower(), raw.strip().lower())


def normalize_goldrush_chain(raw: str) -> str:
    return GOLDRUSH_ALIASES.get(raw.strip().lower(), raw.strip().lower())


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CreditLimitError(ApiError):
    """Raised when a paid indexer is out of credits."""


@dataclass
class Holding:
    address: str
    chain_name: str
    chain_id: int | None
    chain_display: str
    contract: str
    symbol: str
    name: str
    amount: Decimal
    price: Decimal | None
    value: Decimal
    native: bool
    spam: bool
    token_type: str

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["amount"] = format(self.amount, "f")
        payload["price"] = None if self.price is None else format(self.price, "f")
        payload["value"] = format(self.value, "f")
        return payload


@dataclass
class ChainFailure:
    address: str
    chain: str
    error: str


@dataclass
class Portfolio:
    addresses: list[str] = field(default_factory=list)
    holdings: list[Holding] = field(default_factory=list)
    failures: list[ChainFailure] = field(default_factory=list)
    scanned_chains: dict[str, list[str]] = field(default_factory=dict)

    @property
    def total(self) -> Decimal:
        return sum((h.value for h in self.holdings), Decimal("0"))


@dataclass
class ChainAsset:
    chain: str
    name: str
    symbol: str
    contract: str
    quantity: Decimal
    value: Decimal
    native: bool
    wallets: set = field(default_factory=set)

    @property
    def price(self) -> Decimal | None:
        if self.quantity <= 0 or self.value <= 0:
            return None
        return self.value / self.quantity


def grouped_totals(holdings: list[Holding], key) -> list[tuple[str, Decimal]]:
    buckets: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for holding in holdings:
        buckets[key(holding)] += holding.value
    return sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)


def aggregate_assets_by_chain(holdings: list[Holding]) -> list[tuple[str, Decimal, list[ChainAsset]]]:
    buckets: dict[tuple[str, str], ChainAsset] = {}
    for holding in holdings:
        contract = (holding.contract or holding.symbol or "").lower()
        key = (holding.chain_display, contract)
        row = buckets.get(key)
        if row is None:
            row = ChainAsset(
                chain=holding.chain_display,
                name=holding.name or holding.symbol,
                symbol=holding.symbol,
                contract=holding.contract,
                quantity=Decimal("0"),
                value=Decimal("0"),
                native=holding.native,
            )
            buckets[key] = row
        row.quantity += holding.amount
        row.value += holding.value
        row.wallets.add(holding.address)
        if holding.name and (not row.name or len(holding.name) > len(row.name)):
            row.name = holding.name
        if holding.symbol and row.symbol in {"", "?"}:
            row.symbol = holding.symbol

    by_chain: dict[str, list[ChainAsset]] = defaultdict(list)
    for row in buckets.values():
        by_chain[row.chain].append(row)
    for assets in by_chain.values():
        assets.sort(key=lambda item: (item.value, item.quantity), reverse=True)

    ordered = sorted(
        by_chain.items(),
        key=lambda kv: sum((item.value for item in kv[1]), Decimal("0")),
        reverse=True,
    )
    return [
        (chain, sum((item.value for item in assets), Decimal("0")), assets)
        for chain, assets in ordered
    ]


_LLAMA_CACHE: dict[str, Decimal] = {}


def reset_llama_cache() -> None:
    _LLAMA_CACHE.clear()


class RateLimiter:
    def __init__(self, requests_per_second: float = 3.5) -> None:
        self._interval = 1.0 / requests_per_second
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = max(now, self._next) + self._interval


def load_goldrush_key() -> str:
    load_dotenv()
    return (
        os.getenv("GOLDRUSH_API_KEY")
        or os.getenv("COVALENT_API_KEY")
        or os.getenv("CQT_API_KEY")
        or ""
    ).strip()


def parse_address(raw: str) -> str:
    value = raw.strip()
    if not value or value.startswith("#"):
        raise ValueError("empty address")
    if ADDRESS_RE.fullmatch(value) or DOMAIN_RE.fullmatch(value):
        return value
    raise ValueError(f"invalid EVM address or ENS name: {raw!r}")


def extract_addresses(raw: str) -> list[str]:
    """Pull addresses out of a line that may contain commas, labels, or comments."""
    text = raw.split("#", 1)[0].strip()
    if not text:
        return []
    tokens = [part.strip() for part in re.split(r"[\s,;]+", text) if part.strip()]
    found = [token for token in tokens if ADDRESS_RE.fullmatch(token) or DOMAIN_RE.fullmatch(token)]
    if found:
        return found
    if tokens:
        raise ValueError(f"invalid EVM address or ENS name: {raw!r}")
    return []


def load_address_file(file_path: str) -> list[str]:
    path = Path(file_path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
        if isinstance(payload, dict):
            payload = payload.get("addresses") or payload.get("wallets") or []
        rows: list[str] = []
        for item in payload:
            if isinstance(item, str):
                rows.append(item)
            elif isinstance(item, dict):
                rows.append(str(item.get("address") or item.get("wallet") or ""))
        return rows
    return text.splitlines()


def read_addresses(values: Iterable[str], file_path: str | None) -> list[str]:
    collected: list[str] = []
    sources: list[str] = list(ADDRESSES)
    sources.extend(values)
    chosen_file = file_path
    if not chosen_file and Path(DEFAULT_ADDRESS_FILE).exists():
        chosen_file = DEFAULT_ADDRESS_FILE
    if chosen_file:
        sources.extend(load_address_file(chosen_file))
    for item in sources:
        item = str(item).strip()
        if not item or item.startswith("#"):
            continue
        collected.extend(extract_addresses(item))
    seen: set[str] = set()
    unique: list[str] = []
    for addr in collected:
        key = addr.lower()
        if key not in seen:
            seen.add(key)
            unique.append(addr)
    if not unique:
        raise SystemExit(
            "Provide at least one EVM address.\n"
            f"  • Edit ADDRESSES in calculate_assets.py\n"
            f"  • Or put wallets in {DEFAULT_ADDRESS_FILE} (one per line)\n"
            "  • Or pass them: python calculate_assets.py 0xabc... 0xdef..."
        )
    return unique


def to_decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def human_amount(balance: Any, decimals: Any) -> Decimal:
    raw = to_decimal(balance)
    places = int(decimals or 0)
    if places < 0:
        places = 0
    return raw / (Decimal(10) ** places)


def unwrap_goldrush(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("error"):
        message = str(payload.get("error_message") or payload.get("error_message") or "GoldRush API error")
        code = payload.get("error_code") or payload.get("error_code")
        if code == 402 or "credit limit" in message.lower():
            raise CreditLimitError(message, 402)
        raise ApiError(message)
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def money(value: Decimal, currency: str) -> str:
    symbol = CURRENCY_SYMBOLS.get(currency.upper(), f"{currency} ")
    sign = "-" if value < 0 else ""
    return f"{sign}{symbol}{abs(value):,.2f}"


def format_quantity(amount: Decimal) -> str:
    if amount == 0:
        return "0"
    sign = "-" if amount < 0 else ""
    amount = amount.copy_abs()
    if amount >= Decimal("1000000"):
        text = f"{amount:,.2f}"
    elif amount >= Decimal("1"):
        text = f"{amount:,.6f}".rstrip("0").rstrip(".")
    else:
        text = f"{amount:.10f}".rstrip("0").rstrip(".")
        if not text or text == ".":
            text = format(amount.normalize(), "f")
    return sign + text


def short_address(address: str) -> str:
    if address.startswith("0x") and len(address) == 42:
        return f"{address[:6]}...{address[-4:]}"
    return address


def is_evm_chain(name: str) -> bool:
    lowered = name.lower()
    return not any(lowered.startswith(prefix) for prefix in NON_EVM_PREFIXES)


class GoldRushClient:
    def __init__(self, api_key: str, timeout: float = 60.0) -> None:
        self._limiter = RateLimiter()
        self._client = httpx.AsyncClient(
            base_url=GOLDRUSH_BASE,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(5):
            await self._limiter.wait()
            try:
                response = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            if response.status_code == 402:
                raise CreditLimitError(response.text[:300], 402)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 2 * (attempt + 1)))
                await asyncio.sleep(retry_after)
                continue
            if response.status_code >= 500:
                last_error = ApiError(response.text[:300], response.status_code)
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code >= 400:
                raise ApiError(f"HTTP {response.status_code}: {response.text[:300]}", response.status_code)
            return unwrap_goldrush(response.json())
        raise ApiError(f"request failed after retries: {last_error}")

    async def address_activity(self, address: str, testnets: bool = False) -> list[dict[str, Any]]:
        data = await self._get(
            f"/address/{address}/activity/",
            params={"testnets": str(testnets).lower()},
        )
        return list(data.get("items") or [])

    async def token_balances(
        self,
        chain: str,
        address: str,
        currency: str,
        no_spam: bool,
        include_nfts: bool,
    ) -> list[dict[str, Any]]:
        data = await self._get(
            f"/{chain}/address/{address}/balances_v2/",
            params={
                "quote-currency": currency,
                "no-spam": str(no_spam).lower(),
                "nft": str(include_nfts).lower(),
            },
        )
        items = list(data.get("items") or [])
        chain_id = data.get("chain_id")
        chain_name = data.get("chain_name") or chain
        for item in items:
            item.setdefault("chain_id", chain_id)
            item.setdefault("chain_name", chain_name)
        return items


def chain_name_from_activity(item: dict[str, Any]) -> str | None:
    name = item.get("name") or item.get("chain_name")
    if isinstance(name, str) and name:
        return name
    return None


def item_to_holding(address: str, item: dict[str, Any]) -> Holding | None:
    raw_balance = item.get("balance") or "0"
    if str(raw_balance) in {"0", "0x0"}:
        return None
    amount = human_amount(raw_balance, item.get("contract_decimals"))
    if amount <= 0:
        return None
    native = bool(item.get("is_native_token") or item.get("native_token"))
    chain_name = str(item.get("chain_name") or "unknown")
    chain_display = str(
        item.get("chain_display_name")
        or chain_name.replace("-mainnet", "").replace("-", " ").title()
    )
    symbol = str(item.get("contract_ticker_symbol") or ("ETH" if native else "?"))
    name = str(item.get("contract_display_name") or item.get("contract_name") or symbol)
    quote = item.get("quote")
    price = item.get("quote_rate")
    chain_id = item.get("chain_id")
    try:
        chain_id_int = int(chain_id) if chain_id is not None else None
    except (TypeError, ValueError):
        chain_id_int = None
    return Holding(
        address=address,
        chain_name=chain_name,
        chain_id=chain_id_int,
        chain_display=chain_display,
        contract=str(item.get("contract_address") or ("native" if native else "")),
        symbol=symbol,
        name=name,
        amount=amount,
        price=None if price is None else to_decimal(price),
        value=to_decimal(quote),
        native=native,
        spam=bool(item.get("is_spam")),
        token_type=str(item.get("type") or "cryptocurrency"),
    )


def keep_holding(
    holding: Holding,
    *,
    include_spam: bool,
    include_dust: bool,
    include_nfts: bool,
    min_value: Decimal,
    apply_min_value: bool = True,
) -> bool:
    if holding.spam and not include_spam:
        return False
    if holding.token_type == "nft" and not include_nfts:
        return False
    if holding.token_type == "dust" and not include_dust and holding.value <= 0:
        return False
    if apply_min_value and holding.value < min_value:
        return False
    return True


async def resolve_goldrush_chains(
    client: GoldRushClient,
    address: str,
    *,
    explicit: list[str] | None,
    fast: bool,
    testnets: bool,
) -> list[str]:
    if explicit:
        return list(dict.fromkeys(normalize_goldrush_chain(name) for name in explicit))
    chains: list[str] = []
    try:
        activity = await client.address_activity(address, testnets=testnets)
        for item in activity:
            name = chain_name_from_activity(item)
            if not name:
                continue
            if not testnets and "testnet" in name:
                continue
            if is_evm_chain(name):
                chains.append(name)
    except CreditLimitError:
        raise
    except ApiError as exc:
        print(f"warning: could not load chain activity for {address}: {exc}", file=sys.stderr)
    extras = FOUNDATIONAL_CHAINS if fast else EVM_MAINNETS
    chains.extend(extras)
    return list(dict.fromkeys(chains))


async def fetch_goldrush_address(
    client: GoldRushClient,
    address: str,
    chains: list[str],
    args: argparse.Namespace,
) -> tuple[list[Holding], list[ChainFailure]]:
    holdings: list[Holding] = []
    failures: list[ChainFailure] = []
    min_value = Decimal(str(args.min_value))
    exhausted = asyncio.Event()

    async def one_chain(chain: str) -> None:
        if exhausted.is_set():
            return
        try:
            items = await client.token_balances(
                chain=chain,
                address=address,
                currency=args.currency,
                no_spam=not args.include_spam,
                include_nfts=args.include_nfts,
            )
        except CreditLimitError:
            exhausted.set()
            return
        except ApiError as exc:
            if exc.status in {400, 404}:
                return
            failures.append(ChainFailure(address=address, chain=chain, error=str(exc)))
            return
        for item in items:
            holding = item_to_holding(address, item)
            if holding is None:
                continue
            if keep_holding(
                holding,
                include_spam=args.include_spam,
                include_dust=args.include_dust,
                include_nfts=args.include_nfts,
                min_value=min_value,
            ):
                holdings.append(holding)

    await asyncio.gather(*(one_chain(chain) for chain in chains))
    if exhausted.is_set():
        raise CreditLimitError(
            "GoldRush credit limit exceeded. Upgrade at https://goldrush.dev/platform "
            "or omit --provider goldrush to use free Blockscout explorers.",
            402,
        )
    holdings.sort(key=lambda h: h.value, reverse=True)
    return holdings, failures


async def build_goldrush_portfolio(addresses: list[str], args: argparse.Namespace) -> Portfolio:
    key = load_goldrush_key()
    if not key:
        raise SystemExit(
            "Missing GOLDRUSH_API_KEY. Use the default Blockscout provider "
            "(no key required), or set a key from https://goldrush.dev/platform"
        )
    client = GoldRushClient(key)
    portfolio = Portfolio(addresses=list(addresses))
    min_value = Decimal(str(args.min_value))
    include_hl = want_hyperliquid(args)
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as http:
            hl = HyperliquidIndex(http)
            if include_hl:
                await hl._ensure_meta()
            total = len(addresses)
            wallet_sem = asyncio.Semaphore(WALLET_CONCURRENCY)
            progress = {"done": 0}
            progress_lock = asyncio.Lock()

            async def scan_address(address: str) -> tuple[str, list[str], list[Holding], list[ChainFailure]]:
                async with wallet_sem:
                    hl_task = (
                        asyncio.create_task(hl.fetch_address(address)) if include_hl else None
                    )
                    try:
                        chains = await resolve_goldrush_chains(
                            client,
                            address,
                            explicit=args.chains,
                            fast=args.fast,
                            testnets=args.testnets,
                        )
                        holdings, failures = await fetch_goldrush_address(client, address, chains, args)
                        if hl_task is not None:
                            hl_holdings, hl_fail = await hl_task
                            hl_task = None
                            if hl_fail is not None:
                                failures.append(hl_fail)
                            holdings.extend(
                                holding
                                for holding in hl_holdings
                                if keep_hyperliquid_holding(holding, min_value)
                            )
                            if "hyperliquid" not in chains:
                                chains = list(chains) + ["hyperliquid"]
                    finally:
                        if hl_task is not None and not hl_task.done():
                            hl_task.cancel()
                    async with progress_lock:
                        progress["done"] += 1
                        print(
                            f"[{progress['done']}/{total}] {address}  {len(chains)} chain(s)",
                            file=sys.stderr,
                        )
                    return address, chains, holdings, failures

            rows = await asyncio.gather(*(scan_address(address) for address in addresses))
            for address, chains, holdings, failures in rows:
                portfolio.scanned_chains[address] = chains
                portfolio.holdings.extend(holdings)
                portfolio.failures.extend(failures)
    finally:
        await client.aclose()
    portfolio.holdings.sort(key=lambda h: h.value, reverse=True)
    return portfolio


def explorer_chains(args: argparse.Namespace) -> list[ExplorerChain]:
    selected: list[ExplorerChain] = list(BLOCKSCOUT_CHAINS)
    if not args.fast:
        selected.extend(RPC_CHAINS)
    if args.chains:
        wanted = {normalize_explorer_slug(c) for c in args.chains}
        selected = [
            chain
            for chain in ALL_EXPLORER_CHAINS
            if chain.slug in wanted or chain.name.lower() in wanted
        ]
    elif args.fast:
        selected = [chain for chain in BLOCKSCOUT_CHAINS if chain.slug in FAST_EXPLORER_SLUGS]
    return list({chain.slug: chain for chain in selected}.values())


def token_contract(token: dict[str, Any]) -> str:
    return str(token.get("address_hash") or token.get("address") or "").lower()


def explorer_native_holding(
    address: str,
    chain: ExplorerChain,
    payload: dict[str, Any],
    fallback_price: Decimal | None,
) -> Holding | None:
    amount = human_amount(payload.get("coin_balance"), 18)
    if amount <= 0:
        return None
    price = to_decimal(payload.get("exchange_rate"))
    if price <= 0 and fallback_price is not None:
        price = fallback_price
    value = amount * price if price > 0 else Decimal("0")
    return Holding(
        address=address,
        chain_name=chain.slug,
        chain_id=chain.chain_id,
        chain_display=chain.name,
        contract="native",
        symbol=chain.native_symbol,
        name=chain.native_symbol,
        amount=amount,
        price=price if price > 0 else None,
        value=value,
        native=True,
        spam=False,
        token_type="cryptocurrency",
    )


def explorer_token_holding(
    address: str,
    chain: ExplorerChain,
    item: dict[str, Any],
    include_nfts: bool,
) -> Holding | None:
    token = item.get("token") or {}
    token_type = str(token.get("type") or "ERC-20")
    if token_type != "ERC-20" and not include_nfts:
        return None
    amount = human_amount(item.get("value"), token.get("decimals") or 0)
    if amount <= 0:
        return None
    price = to_decimal(token.get("exchange_rate"))
    value = amount * price if price > 0 else Decimal("0")
    symbol = str(token.get("symbol") or "?")
    is_nft = token_type != "ERC-20"
    return Holding(
        address=address,
        chain_name=chain.slug,
        chain_id=chain.chain_id,
        chain_display=chain.name,
        contract=token_contract(token) or "unknown",
        symbol=symbol,
        name=str(token.get("name") or symbol),
        amount=amount,
        price=price if price > 0 else None,
        value=value,
        native=False,
        spam=False,
        token_type="nft" if is_nft else "cryptocurrency",
    )


async def llama_prices(client: httpx.AsyncClient, coins: list[str]) -> dict[str, Decimal]:
    unique = list(dict.fromkeys(coin for coin in coins if coin))
    missing = [coin for coin in unique if coin not in _LLAMA_CACHE]
    if missing:

        async def one_batch(batch: list[str]) -> None:
            url = f"{LLAMA_PRICES}/{','.join(batch)}"
            try:
                response = await client.get(url, timeout=15.0)
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError:
                return
            for key, info in (payload.get("coins") or {}).items():
                price = to_decimal((info or {}).get("price"))
                if price > 0:
                    _LLAMA_CACHE[key] = price

        batches = [missing[i : i + 40] for i in range(0, len(missing), 40)]
        await asyncio.gather(*(one_batch(batch) for batch in batches))
    return {coin: _LLAMA_CACHE[coin] for coin in unique if coin in _LLAMA_CACHE}


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
) -> httpx.Response | None:
    last_resp: httpx.Response | None = None
    for attempt in range(2):
        try:
            response = await client.get(url, headers=headers, timeout=8.0)
        except httpx.HTTPError:
            if attempt == 1:
                return last_resp
            await asyncio.sleep(0.25)
            continue
        last_resp = response
        if response.status_code < 500:
            return response
        if attempt == 0:
            await asyncio.sleep(0.25)
    return last_resp


def legacy_token_holding(
    address: str,
    chain: ExplorerChain,
    item: dict[str, Any],
    include_nfts: bool,
) -> Holding | None:
    token_type = str(item.get("type") or "ERC-20")
    is_erc20 = token_type.upper().startswith("ERC-20") or token_type.upper() == "ERC20"
    if not is_erc20 and not include_nfts:
        return None
    decimals = item.get("decimals")
    if decimals in ("", None):
        decimals = 18 if is_erc20 else 0
    amount = human_amount(item.get("balance"), decimals)
    if amount <= 0:
        return None
    symbol = str(item.get("symbol") or "?")
    return Holding(
        address=address,
        chain_name=chain.slug,
        chain_id=chain.chain_id,
        chain_display=chain.name,
        contract=str(item.get("contractAddress") or item.get("contractaddress") or "").lower(),
        symbol=symbol,
        name=str(item.get("name") or symbol),
        amount=amount,
        price=None,
        value=Decimal("0"),
        native=False,
        spam=False,
        token_type="cryptocurrency" if is_erc20 else "nft",
    )


async def fetch_blockscout_legacy_chain(
    client: httpx.AsyncClient,
    address: str,
    chain: ExplorerChain,
    args: argparse.Namespace,
) -> tuple[list[Holding], ChainFailure | None]:
    """Older Blockscout account API. Used when the v2 explorer returns 5xx."""
    headers = {"Accept": "application/json", "User-Agent": "calculate-assets/1.0"}
    balance_url = f"{chain.explorer}/api?module=account&action=balance&address={address}"
    token_url = f"{chain.explorer}/api?module=account&action=tokenlist&address={address}"
    try:
        balance_resp, token_resp = await asyncio.gather(
            client.get(balance_url, headers=headers),
            client.get(token_url, headers=headers),
        )
    except httpx.HTTPError as exc:
        return [], ChainFailure(
            address=address,
            chain=chain.name,
            error=f"{type(exc).__name__}: {exc or 'legacy explorer request failed'}",
        )

    holdings: list[Holding] = []
    if balance_resp.status_code == 200:
        try:
            payload = balance_resp.json()
        except json.JSONDecodeError:
            payload = {}
        if str(payload.get("status")) == "1" or payload.get("result"):
            native = explorer_native_holding(
                address,
                chain,
                {"coin_balance": payload.get("result"), "exchange_rate": None},
                None,
            )
            if native is not None:
                holdings.append(native)
    elif balance_resp.status_code not in {404}:
        return [], ChainFailure(
            address=address,
            chain=chain.name,
            error=f"legacy address HTTP {balance_resp.status_code}: {balance_resp.text[:120]}",
        )

    if token_resp.status_code == 200:
        try:
            payload = token_resp.json()
        except json.JSONDecodeError:
            payload = {}
        items = payload.get("result") if isinstance(payload, dict) else payload
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                holding = legacy_token_holding(address, chain, item, include_nfts=args.include_nfts)
                if holding is not None:
                    holdings.append(holding)
    return holdings, None


async def fetch_blockscout_chain(
    client: httpx.AsyncClient,
    address: str,
    chain: ExplorerChain,
    args: argparse.Namespace,
) -> tuple[list[Holding], ChainFailure | None]:
    headers = {"Accept": "application/json", "User-Agent": "calculate-assets/1.0"}
    address_resp, token_resp = await asyncio.gather(
        _get_with_retry(client, f"{chain.explorer}/api/v2/addresses/{address}", headers),
        _get_with_retry(client, f"{chain.explorer}/api/v2/addresses/{address}/token-balances", headers),
    )

    v2_down = (
        address_resp is None
        or address_resp.status_code >= 500
        or (token_resp is not None and token_resp.status_code >= 500)
    )
    if v2_down:
        print(f"  {chain.name} v2 explorer failed; trying legacy API ...", file=sys.stderr)
        return await fetch_blockscout_legacy_chain(client, address, chain, args)

    if address_resp.status_code == 404:
        return [], None
    if address_resp.status_code >= 400:
        return [], ChainFailure(
            address=address,
            chain=chain.name,
            error=f"address HTTP {address_resp.status_code}: {address_resp.text[:120]}",
        )

    holdings: list[Holding] = []
    native = explorer_native_holding(address, chain, address_resp.json(), None)
    if native is not None:
        holdings.append(native)

    if token_resp is not None and token_resp.status_code == 200:
        try:
            items = token_resp.json()
        except json.JSONDecodeError:
            items = []
        if isinstance(items, list):
            for item in items:
                holding = explorer_token_holding(address, chain, item, include_nfts=args.include_nfts)
                if holding is not None:
                    holdings.append(holding)
    elif token_resp is not None and token_resp.status_code not in {404, 501}:
        return holdings, ChainFailure(
            address=address,
            chain=chain.name,
            error=f"token balances HTTP {token_resp.status_code}",
        )
    return holdings, None


async def fetch_rpc_native(
    client: httpx.AsyncClient,
    address: str,
    chain: ExplorerChain,
) -> tuple[list[Holding], ChainFailure | None]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [address, "latest"]}
    headers = {"Content-Type": "application/json", "User-Agent": "calculate-assets/1.0"}
    try:
        response = await client.post(chain.explorer, json=payload, headers=headers)
        response.raise_for_status()
        result = (response.json() or {}).get("result")
    except httpx.HTTPError as exc:
        return [], ChainFailure(address=address, chain=chain.name, error=str(exc))
    if not result:
        return [], None
    amount = human_amount(int(result, 16), 18)
    if amount <= 0:
        return [], None
    holding = Holding(
        address=address,
        chain_name=chain.slug,
        chain_id=chain.chain_id,
        chain_display=chain.name,
        contract="native",
        symbol=chain.native_symbol,
        name=chain.native_symbol,
        amount=amount,
        price=None,
        value=Decimal("0"),
        native=True,
        spam=False,
        token_type="cryptocurrency",
    )
    return [holding], None


STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "USDC.E", "USDT0", "WETH", "WBTC", "ETH", "POL", "WPOL", "BNB", "AVAX"}


async def fill_missing_prices(client: httpx.AsyncClient, holdings: list[Holding]) -> None:
    candidates: list[tuple[int, int, str]] = []
    for i, holding in enumerate(holdings):
        if holding.value > 0 or holding.price:
            continue
        llama_slug = LLAMA_BY_SLUG.get(holding.chain_name, holding.chain_name)
        if holding.native:
            coin = f"{llama_slug}:{ZERO_ADDRESS}"
            priority = 0
        elif holding.contract.startswith("0x"):
            coin = f"{llama_slug}:{holding.contract}"
            if holding.symbol.upper() in STABLE_SYMBOLS:
                priority = 1
            elif holding.amount > Decimal("10000000"):
                continue
            else:
                priority = 2
        else:
            continue
        candidates.append((priority, i, coin))
    candidates.sort()
    candidates = candidates[:400]
    if not candidates:
        return
    prices = await llama_prices(client, [coin for _, _, coin in candidates])
    for _, i, coin in candidates:
        price = prices.get(coin)
        if not price:
            continue
        holding = holdings[i]
        holding.price = price
        holding.value = holding.amount * price


def want_hyperliquid(args: argparse.Namespace) -> bool:
    if getattr(args, "no_hyperliquid", False):
        return False
    if not args.chains:
        return True
    wanted = {normalize_explorer_slug(c) for c in args.chains}
    return "hyperliquid" in wanted


class HyperliquidIndex:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._mids: dict[str, Any] | None = None
        self._token_pairs: dict[int, list[str]] | None = None
        self._tokens: dict[int, dict[str, Any]] = {}
        self._unit_evm: list[dict[str, Any]] = []
        self._unit_limit = RateLimiter(UNIT_REQUESTS_PER_SEC)
        self._json_headers = {"Content-Type": "application/json", "User-Agent": "calculate-assets/1.0"}
        self._meta_lock = asyncio.Lock()

    async def _info(self, payload: dict[str, Any]) -> Any:
        response = await self._client.post(
            HYPERLIQUID_INFO,
            json=payload,
            headers=self._json_headers,
            timeout=20.0,
        )
        response.raise_for_status()
        return response.json()

    async def _ensure_meta(self) -> None:
        if self._mids is not None and self._token_pairs is not None:
            return
        async with self._meta_lock:
            if self._mids is not None and self._token_pairs is not None:
                return
            mids, meta = await asyncio.gather(
                self._info({"type": "allMids"}),
                self._info({"type": "spotMeta"}),
            )
            self._mids = mids if isinstance(mids, dict) else {}
            pairs: dict[int, list[str]] = defaultdict(list)
            tokens: dict[int, dict[str, Any]] = {}
            unit_evm: list[dict[str, Any]] = []
            for token in (meta or {}).get("tokens") or []:
                try:
                    index = int(token.get("index"))
                except (TypeError, ValueError):
                    continue
                name = str(token.get("name") or "")
                full_name = str(token.get("fullName") or "")
                tokens[index] = token
                evm = token.get("evmContract") or {}
                contract = str((evm.get("address") if isinstance(evm, dict) else "") or "").lower()
                is_unit = full_name.lower().startswith("unit ") or name.upper() in UNIT_UNDERLYING
                if contract.startswith("0x") and is_unit:
                    extra = 0
                    if isinstance(evm, dict):
                        extra = int(evm.get("evm_extra_wei_decimals") or 0)
                    decimals = int(token.get("weiDecimals") or 18) + extra
                    if decimals <= 0:
                        decimals = 18
                    unit_evm.append(
                        {
                            "index": index,
                            "symbol": name,
                            "name": full_name or name,
                            "contract": contract,
                            "decimals": decimals,
                        }
                    )
            for market in (meta or {}).get("universe") or []:
                market_tokens = market.get("tokens") or []
                name = str(market.get("name") or "")
                if market_tokens and name:
                    pairs[int(market_tokens[0])].append(name)
            self._token_pairs = dict(pairs)
            self._tokens = tokens
            self._unit_evm = unit_evm

    def _token_name(self, coin: str, token_index: int) -> str:
        if coin == "HYPE":
            return "Hyperliquid"
        info = self._tokens.get(token_index) or {}
        full_name = str(info.get("fullName") or "")
        if full_name:
            return full_name
        underlying = UNIT_UNDERLYING.get((coin or "").upper())
        if underlying:
            return f"Unit {underlying}"
        return coin

    def _spot_price(self, coin: str, token_index: int) -> Decimal:
        symbol = (coin or "").upper()
        if symbol in HL_STABLES:
            return Decimal("1")
        mids = self._mids or {}
        underlying = UNIT_UNDERLYING.get(symbol)
        for key in (underlying, coin, symbol, f"@{token_index}"):
            if not key:
                continue
            price = to_decimal(mids.get(key))
            if price > 0:
                return price
        for pair_name in (self._token_pairs or {}).get(token_index, []):
            if "/" in pair_name:
                price = to_decimal(mids.get(pair_name))
                if price > 0:
                    return price
        for pair_name in (self._token_pairs or {}).get(token_index, []):
            price = to_decimal(mids.get(pair_name))
            if price > 0:
                return price
        return Decimal("0")

    def _spot_holding(
        self,
        address: str,
        coin: str,
        token_index: int,
        amount: Decimal,
        chain_display: str,
        contract: str,
        token_type: str,
        name: str | None = None,
        chain_name: str = "hyperliquid",
        chain_id: int | None = None,
    ) -> Holding:
        price = self._spot_price(coin, token_index)
        return Holding(
            address=address,
            chain_name=chain_name,
            chain_id=chain_id,
            chain_display=chain_display,
            contract=contract,
            symbol=coin,
            name=name or self._token_name(coin, token_index),
            amount=amount,
            price=price if price > 0 else None,
            value=amount * price if price > 0 else Decimal("0"),
            native=False,
            spam=False,
            token_type=token_type,
        )

    @staticmethod
    def _spot_amount(item: dict[str, Any]) -> Decimal:
        total = to_decimal(item.get("total"))
        hold = to_decimal(item.get("hold"))
        supplied = to_decimal(item.get("supplied"))
        borrowed = to_decimal(item.get("borrowed"))
        amount = total if total > 0 else hold
        amount += supplied
        amount -= borrowed
        return amount if amount > 0 else Decimal("0")

    async def _rpc_call(self, rpc: str, method: str, params: list[Any]) -> Any:
        response = await self._client.post(
            rpc,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            headers=self._json_headers,
            timeout=20.0,
        )
        response.raise_for_status()
        return (response.json() or {}).get("result")

    async def _erc20_balance(self, rpc: str, token: str, holder: str) -> int:
        data = BALANCE_OF_SELECTOR + "000000000000000000000000" + holder[2:].lower()
        result = await self._rpc_call(rpc, "eth_call", [{"to": token, "data": data}, "latest"])
        if not result or result == "0x":
            return 0
        return int(result, 16)

    async def _erc20_balances(self, rpc: str, tokens: list[dict[str, Any]], holder: str) -> list[int]:
        data = BALANCE_OF_SELECTOR + "000000000000000000000000" + holder[2:].lower()
        batch = [
            {
                "jsonrpc": "2.0",
                "id": index,
                "method": "eth_call",
                "params": [{"to": token["contract"], "data": data}, "latest"],
            }
            for index, token in enumerate(tokens)
        ]
        try:
            response = await self._client.post(rpc, json=batch, headers=self._json_headers, timeout=20.0)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            payload = None
        balances = [0] * len(tokens)
        if isinstance(payload, list) and len(payload) == len(tokens):
            by_id = {item.get("id"): item for item in payload if isinstance(item, dict)}
            for index in range(len(tokens)):
                result = (by_id.get(index) or {}).get("result")
                if result and result != "0x":
                    balances[index] = int(result, 16)
            return balances
        for index, token in enumerate(tokens):
            try:
                balances[index] = await self._erc20_balance(rpc, token["contract"], holder)
            except (httpx.HTTPError, ValueError, TypeError):
                balances[index] = 0
        return balances

    async def _native_balance(self, rpc: str, holder: str) -> int:
        result = await self._rpc_call(rpc, "eth_getBalance", [holder, "latest"])
        if not result or result == "0x":
            return 0
        return int(result, 16)

    async def _unit_get(self, path: str) -> dict[str, Any]:
        url = f"{HYPERUNIT_API}{path}"
        last_error: Exception | None = None
        for attempt in range(5):
            await self._unit_limit.wait()
            try:
                response = await self._client.get(
                    url,
                    headers={"Accept": "application/json", "User-Agent": "calculate-assets/1.0"},
                    timeout=20.0,
                )
            except httpx.HTTPError as exc:
                last_error = exc
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            if response.status_code == 429:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code in {400, 404}:
                return {}
            try:
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last_error = exc
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            return payload if isinstance(payload, dict) else {}
        if last_error:
            print(f"  Unit lookup failed: {last_error}", file=sys.stderr)
        return {}

    async def _unit_operations(self, address: str) -> dict[str, Any]:
        return await self._unit_get(f"/operations/{address}")

    async def _unit_eth_deposit_address(self, address: str) -> str | None:
        payload = await self._unit_get(f"/gen/ethereum/hyperliquid/eth/{address}")
        proto = str(payload.get("address") or "")
        if ADDRESS_RE.fullmatch(proto):
            return proto
        return None

    def _unit_net_amount(self, op: dict[str, Any]) -> Decimal:
        asset = str(op.get("asset") or "").lower()
        decimals = UNIT_ASSET_DECIMALS.get(asset, 18)
        source = to_decimal(op.get("sourceAmount"))
        fees = to_decimal(op.get("destinationFeeAmount")) + to_decimal(op.get("sweepFeeAmount"))
        net = source - fees
        if net <= 0:
            net = source
        return human_amount(net, decimals)

    def _token_index_for_symbol(self, symbol: str) -> int:
        wanted = (symbol or "").upper()
        for index, token in self._tokens.items():
            if str(token.get("name") or "").upper() == wanted:
                return index
        return 0

    async def _unit_and_evm_holdings(self, address: str) -> list[Holding]:
        async def evm_balances() -> list[int]:
            if not self._unit_evm:
                return []
            try:
                return await self._erc20_balances(HYPEREVM_RPC, self._unit_evm, address)
            except (httpx.HTTPError, ValueError, TypeError):
                return []

        raw_balances, unit = await asyncio.gather(evm_balances(), self._unit_operations(address))
        holdings: list[Holding] = []
        for token, raw in zip(self._unit_evm, raw_balances):
            amount = human_amount(raw, token["decimals"])
            if amount <= 0:
                continue
            holdings.append(
                self._spot_holding(
                    address,
                    token["symbol"],
                    token["index"],
                    amount,
                    "HyperEVM",
                    token["contract"],
                    "unit_evm",
                    name=token["name"] or token["symbol"],
                    chain_name="hyperevm",
                    chain_id=999,
                )
            )

        protocol_eth: list[str] = []
        for rec in unit.get("addresses") or []:
            if not isinstance(rec, dict):
                continue
            coin_type = str(rec.get("sourceCoinType") or "").lower()
            dest = str(rec.get("destinationChain") or "").lower()
            proto = str(rec.get("address") or "")
            if dest == "hyperliquid" and coin_type in {"ethereum", "eth"} and ADDRESS_RE.fullmatch(proto):
                protocol_eth.append(proto)

        unique_proto = list(dict.fromkeys(protocol_eth))

        async def proto_balance(proto: str) -> tuple[str, Decimal]:
            try:
                wei = await self._native_balance(ETH_RPC, proto)
            except (httpx.HTTPError, ValueError, TypeError):
                return proto, Decimal("0")
            return proto, human_amount(wei, 18)

        credited_eth = Decimal("0")
        if unique_proto:
            for proto, amount in await asyncio.gather(*(proto_balance(p) for p in unique_proto)):
                if amount <= 0:
                    continue
                credited_eth += amount
                ueth_index = self._token_index_for_symbol("UETH")
                holdings.append(
                    self._spot_holding(
                        address,
                        "UETH",
                        ueth_index,
                        amount,
                        "Hyperliquid Unit",
                        f"unit:eth:{proto.lower()}",
                        "unit_deposit",
                        name="Unit ETH (deposit address)",
                    )
                )

        pending_by_asset: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for op in unit.get("operations") or []:
            if not isinstance(op, dict):
                continue
            dest = str(op.get("destinationChain") or "").lower()
            source = str(op.get("sourceChain") or "").lower()
            state = str(op.get("state") or "")
            if dest != "hyperliquid" or source == "hyperliquid":
                continue
            if state not in UNIT_PENDING_STATES:
                continue
            asset = str(op.get("asset") or "").lower()
            amount = self._unit_net_amount(op)
            if amount <= 0:
                continue
            pending_by_asset[asset] += amount

        if pending_by_asset.get("eth", Decimal("0")) <= credited_eth:
            pending_by_asset.pop("eth", None)
        else:
            pending_by_asset["eth"] -= credited_eth

        for asset, amount in pending_by_asset.items():
            ticker = UNIT_ASSET_TICKER.get(asset, asset.upper())
            token_index = self._token_index_for_symbol(ticker)
            holdings.append(
                self._spot_holding(
                    address,
                    ticker,
                    token_index,
                    amount,
                    "Hyperliquid Unit",
                    f"unit:pending:{asset}",
                    "unit_deposit",
                    name=f"Unit {ticker} (pending deposit)",
                )
            )
        return holdings

    async def fetch_address(self, address: str) -> tuple[list[Holding], ChainFailure | None]:
        if not ADDRESS_RE.fullmatch(address):
            return [], None
        try:
            await self._ensure_meta()

            async def unit_extra() -> list[Holding]:
                try:
                    return await self._unit_and_evm_holdings(address)
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    print(f"  Unit/HyperEVM lookup failed: {exc}", file=sys.stderr)
                    return []

            perps, spot, vaults, extra = await asyncio.gather(
                self._info({"type": "clearinghouseState", "user": address}),
                self._info({"type": "spotClearinghouseState", "user": address}),
                self._info({"type": "userVaultEquities", "user": address}),
                unit_extra(),
            )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            return [], ChainFailure(address=address, chain="Hyperliquid", error=str(exc) or type(exc).__name__)

        holdings: list[Holding] = []
        for item in (spot or {}).get("balances") or []:
            coin = str(item.get("coin") or "UNKNOWN")
            amount = self._spot_amount(item)
            if amount <= 0:
                continue
            token_index = int(item.get("token") or 0)
            holdings.append(
                self._spot_holding(
                    address,
                    coin,
                    token_index,
                    amount,
                    "Hyperliquid Spot",
                    f"spot:{token_index}:{coin}",
                    "spot",
                )
            )
        for item in (spot or {}).get("evmEscrows") or (spot or {}).get("evm_escrows") or []:
            if not isinstance(item, dict):
                continue
            coin = str(item.get("coin") or "UNKNOWN")
            amount = self._spot_amount(item)
            if amount <= 0:
                amount = to_decimal(item.get("amount") or item.get("balance") or item.get("total"))
            if amount <= 0:
                continue
            token_index = int(item.get("token") or 0)
            holdings.append(
                self._spot_holding(
                    address,
                    coin,
                    token_index,
                    amount,
                    "Hyperliquid Spot",
                    f"escrow:{token_index}:{coin}",
                    "spot",
                    name=f"{self._token_name(coin, token_index)} (EVM escrow)",
                )
            )

        summary = (perps or {}).get("marginSummary") or {}
        cash = to_decimal(summary.get("totalRawUsd"))
        account_value = to_decimal(summary.get("accountValue"))
        if cash > 0:
            holdings.append(
                Holding(
                    address=address,
                    chain_name="hyperliquid",
                    chain_id=None,
                    chain_display="Hyperliquid Perps",
                    contract="perp:usdc-margin",
                    symbol="USDC",
                    name="Perp margin (USDC)",
                    amount=cash,
                    price=Decimal("1"),
                    value=cash,
                    native=False,
                    spam=False,
                    token_type="perp_margin",
                )
            )
        for entry in (perps or {}).get("assetPositions") or []:
            position = entry.get("position") or {}
            coin = str(position.get("coin") or "UNKNOWN")
            size = to_decimal(position.get("szi"))
            if size == 0:
                continue
            pnl = to_decimal(position.get("unrealizedPnl"))
            notional = to_decimal(position.get("positionValue")).copy_abs()
            mark = notional / size.copy_abs() if size != 0 and notional > 0 else to_decimal((self._mids or {}).get(coin))
            side = "Long" if size > 0 else "Short"
            holdings.append(
                Holding(
                    address=address,
                    chain_name="hyperliquid",
                    chain_id=None,
                    chain_display="Hyperliquid Perps",
                    contract=f"perp:{coin}:{side.lower()}",
                    symbol=f"{coin}-PERP",
                    name=f"{coin} Perp {side}",
                    amount=size.copy_abs(),
                    price=mark if mark > 0 else None,
                    value=pnl,
                    native=False,
                    spam=False,
                    token_type="perp",
                )
            )
        # If there is equity but no cash/positions parsed, keep account value so it isn't dropped.
        if account_value > 0 and not any(h.token_type in {"perp_margin", "perp"} for h in holdings):
            holdings.append(
                Holding(
                    address=address,
                    chain_name="hyperliquid",
                    chain_id=None,
                    chain_display="Hyperliquid Perps",
                    contract="perp:account-equity",
                    symbol="USDC",
                    name="Perp account equity",
                    amount=account_value,
                    price=Decimal("1"),
                    value=account_value,
                    native=False,
                    spam=False,
                    token_type="perp_margin",
                )
            )

        for vault in vaults or []:
            if not isinstance(vault, dict):
                continue
            equity = to_decimal(vault.get("equity") or vault.get("value") or vault.get("vaultEquity"))
            if equity <= 0:
                continue
            vault_addr = str(vault.get("vaultAddress") or vault.get("vault") or "vault")
            holdings.append(
                Holding(
                    address=address,
                    chain_name="hyperliquid",
                    chain_id=None,
                    chain_display="Hyperliquid Vaults",
                    contract=f"vault:{vault_addr.lower()}",
                    symbol="USDC",
                    name=f"Vault {short_address(vault_addr)}" if vault_addr.startswith("0x") else "Hyperliquid vault",
                    amount=equity,
                    price=Decimal("1"),
                    value=equity,
                    native=False,
                    spam=False,
                    token_type="vault",
                )
            )
        holdings.extend(extra)
        return holdings, None


def keep_hyperliquid_holding(holding: Holding, min_value: Decimal) -> bool:
    if holding.token_type in {"perp", "unit_deposit"}:
        return holding.amount > 0
    return holding.value >= min_value or (holding.amount > 0 and holding.value == 0)


async def build_blockscout_portfolio(addresses: list[str], args: argparse.Namespace) -> Portfolio:
    chains = explorer_chains(args)
    portfolio = Portfolio(addresses=list(addresses))
    min_value = Decimal(str(args.min_value))
    include_hl = want_hyperliquid(args)
    reset_llama_cache()
    limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)
    timeout = httpx.Timeout(connect=5.0, read=12.0, write=10.0, pool=5.0)
    wallet_sem = asyncio.Semaphore(WALLET_CONCURRENCY)
    chain_sem = asyncio.Semaphore(CHAIN_CONCURRENCY)
    progress = {"done": 0}
    progress_lock = asyncio.Lock()
    async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=True) as client:
        hl = HyperliquidIndex(client)
        if include_hl:
            await hl._ensure_meta()
        native_coins = [f"{chain.llama}:{ZERO_ADDRESS}" for chain in chains]
        if native_coins:
            await llama_prices(client, native_coins)
        total = len(addresses)

        async def scan_address(address: str) -> tuple[str, list[Holding], list[ChainFailure], list[str]]:
            async with wallet_sem:
                scanned = [chain.slug for chain in chains]

                async def one(chain: ExplorerChain) -> tuple[list[Holding], ChainFailure | None]:
                    async with chain_sem:
                        if chain.slug in RPC_SLUGS:
                            return await fetch_rpc_native(client, address, chain)
                        holdings, failure = await fetch_blockscout_chain(client, address, chain, args)
                        if failure is not None and chain.slug in RPC_FALLBACKS:
                            rpc_holdings, rpc_failure = await fetch_rpc_native(
                                client, address, RPC_FALLBACKS[chain.slug]
                            )
                            if rpc_holdings:
                                return rpc_holdings, None
                            return holdings, failure or rpc_failure
                        return holdings, failure

                chain_task = asyncio.gather(*(one(chain) for chain in chains)) if chains else asyncio.sleep(0, result=[])
                hl_task = hl.fetch_address(address) if include_hl else asyncio.sleep(0, result=([], None))
                results, hl_pair = await asyncio.gather(chain_task, hl_task)

                found: list[Holding] = []
                failures: list[ChainFailure] = []
                for holdings, failure in results:
                    found.extend(holdings)
                    if failure is not None:
                        failures.append(failure)
                kept = [
                    holding
                    for holding in found
                    if keep_holding(
                        holding,
                        include_spam=args.include_spam,
                        include_dust=args.include_dust,
                        include_nfts=args.include_nfts,
                        min_value=min_value,
                        apply_min_value=False,
                    )
                ]
                hl_holdings, hl_fail = hl_pair
                if hl_fail is not None:
                    failures.append(hl_fail)
                if include_hl:
                    kept.extend(
                        holding
                        for holding in hl_holdings
                        if keep_hyperliquid_holding(holding, min_value)
                    )
                    if "hyperliquid" not in scanned:
                        scanned.append("hyperliquid")
                async with progress_lock:
                    progress["done"] += 1
                    print(
                        f"[{progress['done']}/{total}] {address}  {len(scanned)} chain(s)",
                        file=sys.stderr,
                    )
                return address, kept, failures, scanned

        scanned_rows = await asyncio.gather(*(scan_address(address) for address in addresses))
        for address, kept, failures, scanned in scanned_rows:
            portfolio.scanned_chains[address] = scanned
            portfolio.holdings.extend(kept)
            portfolio.failures.extend(failures)
        await fill_missing_prices(client, portfolio.holdings)
        priced: list[Holding] = []
        for holding in portfolio.holdings:
            if holding.chain_name in {"hyperliquid", "hyperevm"} or holding.token_type in {
                "spot",
                "perp",
                "perp_margin",
                "vault",
                "unit_deposit",
                "unit_evm",
            }:
                if keep_hyperliquid_holding(holding, min_value):
                    priced.append(holding)
            elif holding.value >= min_value:
                priced.append(holding)
        portfolio.holdings = priced
    portfolio.holdings.sort(key=lambda h: h.value, reverse=True)
    return portfolio


def print_table(headers: list[str], rows: list[list[str]], alignments: list[str]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(row: list[str]) -> str:
        parts = []
        for i, cell in enumerate(row):
            parts.append(cell.rjust(widths[i]) if alignments[i] == "r" else cell.ljust(widths[i]))
        return "  ".join(parts)

    print(fmt_row(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt_row(row))


def render_text(portfolio: Portfolio, currency: str, top: int) -> None:
    print()
    print("=" * 88)
    print(" EVM PORTFOLIO VALUE")
    print("=" * 88)
    print(f" Total value : {money(portfolio.total, currency)} {currency}")
    print(f" Addresses   : {len(portfolio.addresses)}")
    print(f" Holdings    : {len(portfolio.holdings)}")
    print()

    print("BY ADDRESS")
    value_by_addr = dict(grouped_totals(portfolio.holdings, lambda h: h.address))
    addr_rows = [
        [addr, money(value_by_addr.get(addr, Decimal("0")), currency)]
        for addr in portfolio.addresses
    ]
    if not addr_rows:
        print("  (no valued holdings)")
    else:
        print_table(["Address", "Value"], addr_rows, ["l", "r"])
    print()

    chain_groups = aggregate_assets_by_chain(portfolio.holdings)
    if not chain_groups:
        print("ASSETS BY CHAIN")
        print("  (no holdings)")
        print()
    else:
        print("ASSETS BY CHAIN")
        print("-" * 88)
        for chain_name, chain_total, assets in chain_groups:
            print()
            print(
                f"{chain_name}  {money(chain_total, currency)}  "
                f"({len(assets)} asset{'' if len(assets) == 1 else 's'})"
            )
            shown = assets if top <= 0 else assets[:top]
            rows = [
                [
                    (asset.name or asset.symbol)[:32],
                    asset.symbol[:12],
                    format_quantity(asset.quantity),
                    money(asset.price, currency) if asset.price is not None else "—",
                    money(asset.value, currency),
                ]
                for asset in shown
            ]
            print_table(
                ["Name", "Symbol", "Quantity", "Price", "Value"],
                rows,
                ["l", "l", "r", "r", "r"],
            )
            hidden = len(assets) - len(shown)
            if hidden > 0:
                print(f"  ... and {hidden} more on {chain_name} (raise --top to see them)")

    unpriced = [h for h in portfolio.holdings if h.value == 0]
    if unpriced:
        print()
        print(f"UNPRICED ASSETS (quantity only, no spot quote): {len(unpriced)}")
        unpriced_rows = [
            [
                (h.name or h.symbol)[:32],
                h.symbol[:12],
                h.chain_display[:16],
                format_quantity(h.amount),
            ]
            for h in unpriced[: max(top, 15)]
        ]
        print_table(["Name", "Symbol", "Chain", "Quantity"], unpriced_rows, ["l", "l", "l", "r"])
        extra = len(unpriced) - len(unpriced_rows)
        if extra > 0:
            print(f"  ... and {extra} more unpriced tokens")

    if portfolio.failures:
        print()
        print("FAILED CHAIN LOOKUPS")
        for failure in portfolio.failures:
            print(f"  {short_address(failure.address)}  {failure.chain}: {failure.error}")
    print()


def chain_asset_json(asset: ChainAsset) -> dict[str, Any]:
    return {
        "name": asset.name,
        "symbol": asset.symbol,
        "quantity": format(asset.quantity, "f"),
        "price": None if asset.price is None else format(asset.price, "f"),
        "value": format(asset.value, "f"),
        "contract": asset.contract,
        "native": asset.native,
        "wallets": sorted(asset.wallets),
    }


def holding_asset_json(holding: Holding) -> dict[str, Any]:
    return {
        "name": holding.name or holding.symbol,
        "symbol": holding.symbol,
        "quantity": format(holding.amount, "f"),
        "price": None if holding.price is None else format(holding.price, "f"),
        "value": format(holding.value, "f"),
        "contract": holding.contract,
        "native": holding.native,
        "chain": holding.chain_display,
        "chain_id": holding.chain_id,
        "token_type": holding.token_type,
    }


def chain_groups_json(holdings: list[Holding]) -> list[dict[str, Any]]:
    return [
        {
            "chain": chain_name,
            "value": format(chain_total, "f"),
            "asset_count": len(assets),
            "assets": [chain_asset_json(asset) for asset in assets],
        }
        for chain_name, chain_total, assets in aggregate_assets_by_chain(holdings)
    ]


def portfolio_json(portfolio: Portfolio, currency: str) -> dict[str, Any]:
    value_by_addr = dict(grouped_totals(portfolio.holdings, lambda h: h.address))
    holdings_by_addr: dict[str, list[Holding]] = defaultdict(list)
    for holding in portfolio.holdings:
        holdings_by_addr[holding.address].append(holding)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "currency": currency,
        "total_value": format(portfolio.total, "f"),
        "address_count": len(portfolio.addresses),
        "addresses": portfolio.addresses,
        "by_address": [
            {
                "address": addr,
                "value": format(value_by_addr.get(addr, Decimal("0")), "f"),
                "holding_count": len(holdings_by_addr.get(addr, [])),
                "by_chain": chain_groups_json(holdings_by_addr.get(addr, [])),
                "holdings": [
                    holding_asset_json(holding)
                    for holding in sorted(
                        holdings_by_addr.get(addr, []),
                        key=lambda item: item.value,
                        reverse=True,
                    )
                ],
            }
            for addr in portfolio.addresses
        ],
        "by_chain": chain_groups_json(portfolio.holdings),
        "holdings": [holding.to_json() for holding in portfolio.holdings],
        "scanned_chains": portfolio.scanned_chains,
        "failures": [asdict(failure) for failure in portfolio.failures],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calculate native + ERC-20 asset value for EVM addresses across chains.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "addresses",
        nargs="*",
        help="EVM addresses or ENS names. Also reads ADDRESSES in this file and addresses.txt",
    )
    parser.add_argument(
        "--file",
        "-f",
        help=f"Text/JSON file of wallets (default: {DEFAULT_ADDRESS_FILE} if present)",
    )
    parser.add_argument(
        "--provider",
        choices=("blockscout", "goldrush", "auto"),
        default="blockscout",
        help="blockscout is free (no key). goldrush needs GOLDRUSH_API_KEY.",
    )
    parser.add_argument("--currency", default="USD", help="Fiat quote currency (USD, EUR, GBP, INR, ...)")
    parser.add_argument(
        "--chains",
        help="Comma-separated chain slugs, e.g. ethereum,polygon,base,arbitrum (matic also works)",
    )
    parser.add_argument("--fast", action="store_true", help="Only query major EVM chains")
    parser.add_argument("--testnets", action="store_true", help="Include testnet activity (GoldRush only)")
    parser.add_argument("--include-spam", action="store_true", help="Keep suspected spam tokens")
    parser.add_argument("--include-dust", action="store_true", help="Keep dust-classified tokens")
    parser.add_argument("--include-nfts", action="store_true", help="Include NFT balances")
    parser.add_argument(
        "--no-hyperliquid",
        action="store_true",
        help="Skip Hyperliquid DEX spot, perps, vaults, and Unit deposits",
    )
    parser.add_argument("--min-value", type=float, default=0.01, help="Hide holdings worth less than this")
    parser.add_argument(
        "--top",
        type=int,
        default=50,
        help="Max assets to print per chain (0 = all)",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON to stdout instead of a table")
    parser.add_argument(
        "--out",
        default=DEFAULT_JSON_OUT,
        help="Write the full JSON report to this path",
    )
    args = parser.parse_args(argv)
    args.currency = args.currency.upper()
    args.chains = [c.strip() for c in args.chains.split(",") if c.strip()] if args.chains else None
    return args


async def build_portfolio(addresses: list[str], args: argparse.Namespace) -> Portfolio:
    provider = args.provider
    if provider == "auto":
        provider = "goldrush" if load_goldrush_key() else "blockscout"
    if provider == "goldrush":
        try:
            return await build_goldrush_portfolio(addresses, args)
        except CreditLimitError as exc:
            if args.provider == "goldrush":
                raise SystemExit(
                    f"GoldRush credits are exhausted: {exc}\n"
                    "Re-run without --provider goldrush to use free Blockscout explorers."
                ) from exc
            print(
                "GoldRush credits are exhausted; falling back to free Blockscout explorers.",
                file=sys.stderr,
            )
            return await build_blockscout_portfolio(addresses, args)
    return await build_blockscout_portfolio(addresses, args)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        addresses = read_addresses(args.addresses, args.file)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: could not read address file: {exc}", file=sys.stderr)
        return 2

    portfolio = asyncio.run(build_portfolio(addresses, args))
    report = portfolio_json(portfolio, args.currency)
    out_path = Path(args.out)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {out_path.resolve()}", file=sys.stderr)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        render_text(portfolio, args.currency, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
