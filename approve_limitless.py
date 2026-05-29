"""一次性:Approve USDC + CTF 給 Limitless Exchange 合約。

跑法:
    .venv/bin/python approve_limitless.py

做什麼:
1. USDC.approve(LM_EXCHANGE, MAX_UINT256)  ← 讓 LM 合約能花你的 USDC(掛 BUY 單時)
2. CTF.setApprovalForAll(LM_EXCHANGE, True) ← 讓 LM 合約能轉你的 YES/NO token(掛 SELL 清庫存時)

成本:每筆 ~$0.005 gas,合計 ~$0.01
頻率:一次性,wallet 換新才需要重做
"""

import os
import sys
import time
import httpx
from dotenv import dotenv_values
from eth_account import Account
from eth_account.signers.local import LocalAccount


BASE_RPC = "https://mainnet.base.org"
CHAIN_ID = 8453

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CTF = "0xC9c98965297Bc527861c898329Ee280632B76e18"
LM_EXCHANGE = "0xe3E00BA3a9888d1DE4834269f62ac008b4BB5C47"

MAX_UINT256 = 2**256 - 1


def rpc(method: str, params: list) -> dict:
    r = httpx.post(BASE_RPC, json={
        "jsonrpc": "2.0", "method": method, "params": params, "id": 1,
    }, timeout=30).json()
    if "error" in r:
        raise RuntimeError(f"RPC error: {r['error']}")
    return r["result"]


def encode_approve(spender: str, amount: int) -> str:
    """ERC-20 approve(spender, amount) calldata。"""
    return "0x095ea7b3" + spender[2:].zfill(64) + hex(amount)[2:].zfill(64)


def encode_set_approval_for_all(operator: str, approved: bool) -> str:
    """ERC-1155 setApprovalForAll(operator, approved) calldata。"""
    return "0xa22cb465" + operator[2:].zfill(64) + ("1" if approved else "0").zfill(64)


def send_tx(acct: LocalAccount, to: str, data: str, label: str) -> str:
    """簽 + 廣播。回傳 tx hash。"""
    nonce = int(rpc("eth_getTransactionCount", [acct.address, "pending"]), 16)
    gas_price = int(rpc("eth_gasPrice", []), 16)

    # estimate gas
    try:
        gas_est = int(rpc("eth_estimateGas", [{
            "from": acct.address, "to": to, "data": data,
        }]), 16)
    except Exception as e:
        print(f"  ⚠️ estimate gas 失敗: {e}")
        gas_est = 100_000

    gas_limit = int(gas_est * 1.2)  # 加 20% buffer
    cost_eth = gas_limit * gas_price / 1e18
    cost_usd = cost_eth * 3500  # 粗估 ETH price

    print(f"  [{label}]")
    print(f"    to:        {to}")
    print(f"    data:      {data[:30]}...({len(data)} chars)")
    print(f"    nonce:     {nonce}")
    print(f"    gas:       {gas_limit:,} × {gas_price/1e9:.4f} gwei")
    print(f"    cost:      {cost_eth:.8f} ETH (~${cost_usd:.4f})")

    tx = {
        "to": to,
        "data": data,
        "nonce": nonce,
        "gas": gas_limit,
        "gasPrice": gas_price,
        "chainId": CHAIN_ID,
        "value": 0,
    }
    signed = acct.sign_transaction(tx)
    raw_hex = signed.raw_transaction.hex()
    if not raw_hex.startswith("0x"):
        raw_hex = "0x" + raw_hex
    tx_hash = rpc("eth_sendRawTransaction", [raw_hex])
    print(f"    ⏳ tx hash:   {tx_hash}")
    print(f"    🔗 basescan: https://basescan.org/tx/{tx_hash}")

    # wait for confirmation
    print(f"    等待 confirmation...", end="", flush=True)
    for _ in range(60):
        time.sleep(2)
        try:
            receipt = rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                status = int(receipt.get("status", "0x0"), 16)
                if status == 1:
                    print(f" ✅ confirmed!")
                    return tx_hash
                else:
                    print(f" ❌ FAILED on-chain")
                    raise RuntimeError(f"Tx {tx_hash} reverted")
            print(".", end="", flush=True)
        except Exception as e:
            print(f" ❌ {e}")
            raise
    raise TimeoutError(f"Tx {tx_hash} 等了 120s 還沒 confirm")


def check_allowance(owner: str) -> int:
    """查 USDC.allowance(owner, LM_EXCHANGE)。"""
    data = "0xdd62ed3e" + owner[2:].zfill(64) + LM_EXCHANGE[2:].zfill(64)
    r = rpc("eth_call", [{"to": USDC, "data": data}, "latest"])
    return int(r, 16)


def check_ctf_approval(owner: str) -> bool:
    """查 CTF.isApprovedForAll(owner, LM_EXCHANGE)。"""
    data = "0xe985e9c5" + owner[2:].zfill(64) + LM_EXCHANGE[2:].zfill(64)
    r = rpc("eth_call", [{"to": CTF, "data": data}, "latest"])
    return int(r, 16) == 1


def main():
    env = dotenv_values(".env")
    priv = env.get("BASE_PRIVATE_KEY")
    if not priv:
        print("❌ BASE_PRIVATE_KEY 沒設")
        sys.exit(1)
    acct: LocalAccount = Account.from_key(priv)
    print(f"Wallet: {acct.address}")
    print()

    # ETH balance
    eth_balance = int(rpc("eth_getBalance", [acct.address, "latest"]), 16) / 1e18
    print(f"ETH 餘額: {eth_balance:.6f}")
    if eth_balance < 0.0001:
        print("❌ ETH 不夠(< 0.0001),先去幣安提一點到這個地址")
        sys.exit(1)
    print()

    # USDC approve(如果還沒設)
    cur_allowance = check_allowance(acct.address)
    print(f"=== USDC.approve(LM Exchange, MAX) ===")
    print(f"  目前 allowance: {cur_allowance/1e6:.2f} USDC")
    if cur_allowance < 10**18:  # 1e12 USDC
        send_tx(acct, USDC, encode_approve(LM_EXCHANGE, MAX_UINT256),
                "USDC approve")
        new_allowance = check_allowance(acct.address)
        print(f"  新 allowance: {new_allowance/1e6:.0f} USDC")
    else:
        print(f"  ✓ 已 approve,跳過")
    print()

    # CTF setApprovalForAll(如果還沒設)
    cur_ctf = check_ctf_approval(acct.address)
    print(f"=== CTF.setApprovalForAll(LM Exchange, true) ===")
    print(f"  目前 approval: {cur_ctf}")
    if not cur_ctf:
        send_tx(acct, CTF, encode_set_approval_for_all(LM_EXCHANGE, True),
                "CTF approval")
        new_ctf = check_ctf_approval(acct.address)
        print(f"  新 approval: {new_ctf}")
    else:
        print(f"  ✓ 已 approve,跳過")
    print()

    print("✅ 全部 approve 完成,可以開始下單")


if __name__ == "__main__":
    main()
