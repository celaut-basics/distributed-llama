#!/usr/bin/env python3
"""What one run cost, from the node's own ledger (read-only):

    ledger.py <database.sqlite> <head instance id>

The accounts are the head and its children (local_instances.father_id, and on a node that
gave the workers to a peer, the rows of delegated_instances that name the head). For each
account it sums the movements by kind. `spent_mu` is what the node charged for resources,
builds and tunnels, over all the accounts: the cost of the run. The movements that only
move MU between the head and its workers (child_start, refund, top_up, child_bound) are
shown but not counted twice.
"""

import json
import sqlite3
import sys

CHARGES = ("maintenance", "build", "tunnel", "resource_change", "start_fee")


def main():
    db, head = sys.argv[1], sys.argv[2]
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    accounts = {head: "head"}
    for (child,) in c.execute("SELECT id FROM local_instances WHERE father_id = ?", (head,)):
        accounts[child] = "worker"
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "delegated_instances" in tables:
        cols = [r[1] for r in c.execute("PRAGMA table_info(delegated_instances)")]
        father = next((x for x in ("father_id", "parent_id", "client_id") if x in cols), None)
        if father:
            for (child,) in c.execute("SELECT id FROM delegated_instances WHERE %s = ?" % father, (head,)):
                accounts[child] = "delegated-worker"
    out = {"accounts": {}, "spent_mu": 0, "lost_mu": 0}
    for account, role in accounts.items():
        kinds = {}
        for kind, amount in c.execute(
                "SELECT kind, SUM(amount_mu) FROM balance_movements WHERE account_id = ? GROUP BY kind",
                (account,)):
            kinds[kind] = amount
        out["accounts"][account] = {"role": role, "by_kind": kinds}
        out["spent_mu"] += -sum(v for k, v in kinds.items() if k in CHARGES and v < 0)
        # A refund to a father that is gone is lost: {"lost": "father_gone"} in its detail.
        for (amount,) in c.execute(
                "SELECT amount_mu FROM balance_movements WHERE account_id = ? AND kind = 'refund' "
                "AND detail LIKE '%lost%'", (account,)):
            out["lost_mu"] += -int(amount)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
