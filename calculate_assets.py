#!/usr/bin/env python3
"""Calculate the USD value of assets for EVM addresses across chains.

Default source: public Blockscout explorers (no API key).
Optional: GoldRush if you still have credits.

Examples:
  python calculate_assets.py 0xd8dA6B...........415D37aA96045
  python calculate_assets.py 0xabc... 0xdef... --json
  python calculate_assets.py --file wallets.txt --min-value 1 --out report.json
  python calculate_assets.py 0xabc... --provider goldrush
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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import httpx
from dotenv import load_dotenv

GOLDRUSH_BASE = "https://api.covalenthq.com/v1"
LLAMA_PRICES = "https://coins.llama.fi/prices/current"
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
DOMAIN_RE = re.compile(r"^[a-zA-Z0-9.-]+\.(eth|lens|crypto|nft|wallet|dao)$", re.I)
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

EVM_MAINNETS: tuple[str, ...] = (
    "eth-mainnet",
    "matic-mainnet",
    "bsc-mainnet",
    "gnosis-mainnet",
    "optimism-mainnet",
    "base-mainnet",
    "arbitrum-mainnet",
    "arbitrum-nova-mainnet",
    "avalanche-mainnet",
    "linea-mainnet",
    "scroll-mainnet",
    "zksync-mainnet",
    "blast-mainnet",
    "mantle-mainnet",
    "taiko-mainnet",
    "unichain-mainnet",
    "world-mainnet",
    "sonic-mainnet",
    "sei-mainnet",
    "berachain-mainnet",
    "hyperevm-mainnet",
    "ink-mainnet",
    "apechain-mainnet",
    "plasma-mainnet",
    "celo-mainnet",
    "fantom-mainnet",
    "moonbeam-mainnet",
    "moonbeam-moonriver",
    "cronos-mainnet",
    "cronos-zkevm-mainnet",
    "bnb-opbnb-mainnet",
    "zetachain-mainnet",
    "redstone-mainnet",
    "canto-mainnet",
    "viction-mainnet",
    "adi-mainnet",
    "axie-mainnet",
    "oasis-sapphire-mainnet",
    "emerald-paratime-mainnet",
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
    ExplorerChain("gnosis", "Gnosis", 100, "https://gnosis.blockscout.com", "xDAI", "xdai"),
    ExplorerChain("scroll", "Scroll", 534352, "https://scroll.blockscout.com", "ETH", "scroll"),
    ExplorerChain("zksync", "zkSync Era", 324, "https://zksync.blockscout.com", "ETH", "era"),
    ExplorerChain("celo", "Celo", 42220, "https://explorer.celo.org", "CELO", "celo"),
    ExplorerChain("unichain", "Unichain", 130, "https://unichain.blockscout.com", "ETH", "unichain"),
    ExplorerChain("ink", "Ink", 57073, "https://explorer.inkonchain.com", "ETH", "ink"),
    ExplorerChain(
        "worldchain",
        "World Chain",
        480,
        "https://worldchain-mainnet.explorer.alchemy.com",
        "ETH",
        "wc",
    ),
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
    holdings: list[Holding] = field(default_factory=list)
    failures: list[ChainFailure] = field(default_factory=list)
    scanned_chains: dict[str, list[str]] = field(default_factory=dict)

    @property
    def total(self) -> Decimal:
        return sum((h.value for h in self.holdings), Decimal("0"))


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


def read_addresses(values: Iterable[str], file_path: str | None) -> list[str]:
    collected: list[str] = []
    sources = list(values)
    if file_path:
        text = Path(file_path).read_text(encoding="utf-8")
        sources.extend(line.strip() for line in text.splitlines())
    for item in sources:
        item = item.strip()
        if not item or item.startswith("#"):
            continue
        collected.append(parse_address(item))
    seen: set[str] = set()
    unique: list[str] = []
    for addr in collected:
        key = addr.lower()
        if key not in seen:
            seen.add(key)
            unique.append(addr)
    if not unique:
        raise SystemExit("Provide at least one EVM address (or ENS name).")
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
) -> bool:
    if holding.spam and not include_spam:
        return False
    if holding.token_type == "nft" and not include_nfts:
        return False
    if holding.token_type == "dust" and not include_dust and holding.value <= 0:
        return False
    if holding.value < min_value:
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
    portfolio = Portfolio()
    try:
        for address in addresses:
            print(f"Scanning {address} via GoldRush ...", file=sys.stderr)
            chains = await resolve_goldrush_chains(
                client,
                address,
                explicit=args.chains,
                fast=args.fast,
                testnets=args.testnets,
            )
            portfolio.scanned_chains[address] = chains
            print(f"  {len(chains)} chain(s)", file=sys.stderr)
            holdings, failures = await fetch_goldrush_address(client, address, chains, args)
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
    if not unique:
        return {}
    out: dict[str, Decimal] = {}
    for i in range(0, len(unique), 40):
        batch = unique[i : i + 40]
        url = f"{LLAMA_PRICES}/{','.join(batch)}"
        try:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError:
            continue
        for key, info in (payload.get("coins") or {}).items():
            price = to_decimal((info or {}).get("price"))
            if price > 0:
                out[key] = price
    return out


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
) -> httpx.Response | None:
    last_resp: httpx.Response | None = None
    for attempt in range(2):
        try:
            response = await client.get(url, headers=headers, timeout=12.0)
        except httpx.HTTPError:
            if attempt == 1:
                return last_resp
            await asyncio.sleep(0.8)
            continue
        last_resp = response
        if response.status_code < 500:
            return response
        if attempt == 0:
            await asyncio.sleep(0.8)
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
    coin = f"{chain.llama}:{ZERO_ADDRESS}"
    prices = await llama_prices(client, [coin])
    price = prices.get(coin) or Decimal("0")
    holding = Holding(
        address=address,
        chain_name=chain.slug,
        chain_id=chain.chain_id,
        chain_display=chain.name,
        contract="native",
        symbol=chain.native_symbol,
        name=chain.native_symbol,
        amount=amount,
        price=price if price > 0 else None,
        value=amount * price if price > 0 else Decimal("0"),
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
    candidates = candidates[:80]
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


async def build_blockscout_portfolio(addresses: list[str], args: argparse.Namespace) -> Portfolio:
    chains = explorer_chains(args)
    portfolio = Portfolio()
    min_value = Decimal(str(args.min_value))
    limits = httpx.Limits(max_connections=12, max_keepalive_connections=12)
    async with httpx.AsyncClient(timeout=60.0, limits=limits, follow_redirects=True) as client:
        for address in addresses:
            print(f"Scanning {address} via Blockscout ...", file=sys.stderr)
            portfolio.scanned_chains[address] = [chain.slug for chain in chains]
            print(f"  {len(chains)} chain(s)", file=sys.stderr)

            async def one(chain: ExplorerChain) -> tuple[list[Holding], ChainFailure | None]:
                if chain.slug in RPC_SLUGS:
                    return await fetch_rpc_native(client, address, chain)
                holdings, failure = await fetch_blockscout_chain(client, address, chain, args)
                if failure is not None and chain.slug in RPC_FALLBACKS:
                    rpc_holdings, rpc_failure = await fetch_rpc_native(
                        client, address, RPC_FALLBACKS[chain.slug]
                    )
                    if rpc_holdings:
                        print(
                            f"  {chain.name} explorer failed; using native RPC fallback ...",
                            file=sys.stderr,
                        )
                        return rpc_holdings, None
                    return holdings, failure or rpc_failure
                return holdings, failure

            results = await asyncio.gather(*(one(chain) for chain in chains))
            found: list[Holding] = []
            for holdings, failure in results:
                found.extend(holdings)
                if failure is not None:
                    portfolio.failures.append(failure)
            await fill_missing_prices(client, found)
            portfolio.holdings.extend(
                holding
                for holding in found
                if keep_holding(
                    holding,
                    include_spam=args.include_spam,
                    include_dust=args.include_dust,
                    include_nfts=args.include_nfts,
                    min_value=min_value,
                )
            )
    portfolio.holdings.sort(key=lambda h: h.value, reverse=True)
    return portfolio


def grouped_totals(holdings: list[Holding], key) -> list[tuple[str, Decimal]]:
    buckets: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for holding in holdings:
        buckets[key(holding)] += holding.value
    return sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)


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
    print("=" * 78)
    print(" EVM PORTFOLIO VALUE".ljust(78))
    print("=" * 78)
    print(f" Total value : {money(portfolio.total, currency)} {currency}")
    print(f" Addresses   : {len(portfolio.scanned_chains)}")
    print(f" Holdings    : {len(portfolio.holdings)}")
    print()

    print("BY ADDRESS")
    addr_rows = [
        [short_address(addr), money(total, currency)]
        for addr, total in grouped_totals(portfolio.holdings, lambda h: h.address)
    ]
    if not addr_rows:
        print("  (no valued holdings)")
    else:
        print_table(["Address", "Value"], addr_rows, ["l", "r"])
    print()

    print("BY CHAIN")
    chain_rows = [
        [name, money(total, currency)]
        for name, total in grouped_totals(portfolio.holdings, lambda h: h.chain_display)
        if total > 0
    ]
    if not chain_rows:
        print("  (no valued holdings)")
    else:
        print_table(["Chain", "Value"], chain_rows, ["l", "r"])
    print()

    print(f"HOLDINGS (top {top})")
    shown = [h for h in portfolio.holdings if h.value > 0][:top]
    hold_rows = [
        [
            h.symbol[:16],
            h.chain_display[:18],
            short_address(h.address),
            f"{h.amount.normalize():f}"[:18],
            money(h.value, currency),
        ]
        for h in shown
    ]
    if not hold_rows:
        print("  (no valued holdings)")
    else:
        print_table(["Token", "Chain", "Wallet", "Amount", "Value"], hold_rows, ["l", "l", "l", "r", "r"])

    unpriced = [h for h in portfolio.holdings if h.value == 0]
    if unpriced:
        print()
        print(f"Unpriced tokens (no spot quote): {len(unpriced)}")
        for holding in unpriced[:15]:
            print(f"  {holding.symbol:12} {holding.chain_display:18} {holding.amount.normalize():f}")
        if len(unpriced) > 15:
            print(f"  ... and {len(unpriced) - 15} more")

    if portfolio.failures:
        print()
        print("FAILED CHAIN LOOKUPS")
        for failure in portfolio.failures:
            print(f"  {short_address(failure.address)}  {failure.chain}: {failure.error}")
    print()


def portfolio_json(portfolio: Portfolio, currency: str) -> dict[str, Any]:
    return {
        "currency": currency,
        "total_value": format(portfolio.total, "f"),
        "by_address": [
            {"address": addr, "value": format(total, "f")}
            for addr, total in grouped_totals(portfolio.holdings, lambda h: h.address)
        ],
        "by_chain": [
            {"chain": name, "value": format(total, "f")}
            for name, total in grouped_totals(portfolio.holdings, lambda h: h.chain_display)
        ],
        "holdings": [h.to_json() for h in portfolio.holdings],
        "scanned_chains": portfolio.scanned_chains,
        "failures": [asdict(f) for f in portfolio.failures],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calculate native + ERC-20 asset value for EVM addresses across chains.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("addresses", nargs="*", help="EVM addresses or ENS names")
    parser.add_argument("--file", "-f", help="Text file with one address per line")
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
    parser.add_argument("--min-value", type=float, default=0.01, help="Hide holdings worth less than this")
    parser.add_argument("--top", type=int, default=25, help="How many holdings to print")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    parser.add_argument("--out", help="Write JSON report to this path")
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
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote {args.out}", file=sys.stderr)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        render_text(portfolio, args.currency, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
