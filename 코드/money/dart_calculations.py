"""The document's fixed formulas, with original inputs and explicit missing states."""
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext

from .dart_data import amount, metrics, row_key

PROCESSOR_VERSION = 2
DEFINITIONS = {
    "revenue": ("ifrs-full_Revenue", ("IS", "CIS")),
    "operating_profit": ("dart_OperatingIncomeLoss", ("IS", "CIS")),
    "net_profit": ("ifrs-full_ProfitLoss", ("IS", "CIS")),
    "liabilities": ("ifrs-full_Liabilities", ("BS",)),
    "equity": ("ifrs-full_Equity", ("BS",)),
    "current_assets": ("ifrs-full_CurrentAssets", ("BS",)),
    "current_liabilities": ("ifrs-full_CurrentLiabilities", ("BS",)),
    "operating_cash_flow": ("ifrs-full_CashFlowsFromUsedInOperatingActivities", ("CF",)),
    "ppe_purchases": ("ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", ("CF",)),
    "intangible_purchases": ("ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities", ("CF",)),
}


def missing(value):
    return value is None or isinstance(value, str) and value.strip() in ("", "-", "—", "–")


def rounded(value):
    with localcontext() as ctx:
        ctx.prec = max(50, value.adjusted() + 12)
        return str(value.quantize(Decimal("0.0001")))


def financial_rows(rows, params):
    """Compare the SAME row's provider-defined corresponding amount fields.

    Annual/BS use frmtrm_amount. Interim flows use frmtrm_q_amount;
    cumulative IS/CIS uses only the two explicitly cumulative fields.
    Never compare a quarter with a prior full year as a fallback.
    """
    keys = Counter(row_key(r) for r in rows)
    output = []
    for index, row in enumerate(rows):
        report = str(params.get("reprt_code") or row.get("reprt_code") or "")
        basis = "annual" if report == "11011" else "balance_dates" if row.get("sj_div") == "BS" else "interim_corresponding"
        before = "frmtrm_amount" if basis in ("annual", "balance_dates") else "frmtrm_q_amount"
        pairs = [("thstrm_amount", before, basis)]
        if report != "11011" and row.get("sj_div") in ("IS", "CIS"):
            pairs.append(("thstrm_add_amount", "frmtrm_add_amount", "cumulative"))
        comparisons = []
        for current, previous, scope in pairs:
            a, b = amount(row.get(current)), amount(row.get(previous))
            result = {"scope": scope, "current_field": current, "previous_field": previous,
                      "current_value": str(a) if a is not None else None,
                      "previous_value": str(b) if b is not None else None,
                      "current_period": row.get("thstrm_nm"),
                      "previous_period": row.get("frmtrm_nm" if previous == "frmtrm_amount" else "frmtrm_q_nm"),
                      "currency": row.get("currency"), "row_index": index,
                      "change_amount": None, "change_pct": None}
            if report not in ("11011", "11013", "11012", "11014"):
                result.update(status="unavailable", reason="report_scope_missing")
            elif keys[row_key(row)] != 1:
                result.update(status="unavailable", reason="ambiguous_account_row")
            elif missing(row.get("currency")):
                result.update(status="unavailable", reason="currency_missing")
            elif a is None or b is None:
                result.update(status="unavailable", reason="amount_missing", missing_fields=[
                    field for field, number in ((current, a), (previous, b)) if number is None])
            else:
                result.update(status="calculated", change_amount=str(a - b),
                              amount_formula="current - previous", pct_formula="(current - previous) / previous * 100",
                              negative_denominator=b < 0)
                if b == 0:
                    result["pct_status"] = "zero_denominator"
                else:
                    try:
                        result.update(change_pct=rounded((a-b)/b*100), pct_status="calculated")
                    except InvalidOperation:
                        result["pct_status"] = "numeric_range_error"
            comparisons.append(result)
        output.append(comparisons)
    return output


def financial_summary(payload, params, comparisons):
    rows = payload.get("list", [])
    inputs, absent = {}, {}
    for name, (account_id, statements) in DEFINITIONS.items():
        matches = [(i, r) for i, r in enumerate(rows) if r.get("account_id") == account_id and r.get("sj_div") in statements]
        if any(r.get("sj_div") == "IS" for _, r in matches):
            matches = [(i, r) for i, r in matches if r.get("sj_div") == "IS"]
        if len(matches) != 1:
            absent[name] = "ambiguous_account" if matches else "account_not_provided"
            continue
        index, row = matches[0]
        number = amount(row.get("thstrm_amount"))
        if number is None or missing(row.get("currency")):
            absent[name] = "amount_missing" if number is None else "currency_missing"
            continue
        inputs[name] = {"value": str(number), "account_id": account_id, "account_nm": row.get("account_nm"),
            "statement": row["sj_div"], "currency": row["currency"], "amount_field": "thstrm_amount",
            "period_label": row.get("thstrm_nm"), "receipt": row.get("rcept_no"), "row_index": index,
            "corp_code": row.get("corp_code", params.get("corp_code")),
            "business_year": row.get("bsns_year", params.get("bsns_year")),
            "report_code": row.get("reprt_code", params.get("reprt_code")), "fs_div": params.get("fs_div")}

    def evaluate(names, formula, cash=False):
        result = {"status": "unavailable", "value": None, "inputs": list(names), "formula": formula}
        missing_names = [n for n in names if n not in inputs]
        if missing_names:
            result.update(reason="input_missing", missing_inputs={n: absent[n] for n in missing_names})
            return result
        values = [inputs[n] for n in names]
        for field in ("currency", "statement", "corp_code", "business_year", "report_code", "fs_div"):
            if len({v[field] for v in values}) != 1:
                result["reason"] = field + "_mismatch"
                return result
        for field in ("period_label", "receipt"):
            known = {v[field] for v in values if not missing(v[field])}
            if len(known) > 1:
                result["reason"] = field + "_mismatch"
                return result
        numbers = [Decimal(v["value"]) for v in values]
        result["currency"] = values[0]["currency"]
        if cash:
            # The document explicitly defines acquisition spending as a
            # positive cash payment. Original signs remain in calculation_inputs.
            result.update(status="calculated", value=str(numbers[0]-abs(numbers[1])-abs(numbers[2])),
                          acquisition_outflow_normalization="absolute_value_as_positive_payment",
                          normalized_outflows=[str(abs(n)) for n in numbers[1:]])
        elif numbers[1] == 0:
            result["reason"] = "zero_denominator"
        else:
            try:
                result.update(status="calculated", value=rounded(numbers[0]/numbers[1]*100),
                              negative_denominator=numbers[1] < 0)
            except InvalidOperation:
                result["reason"] = "numeric_range_error"
        return result

    definitions = {
        "operating_margin_pct": (("operating_profit", "revenue"), "operating_profit / revenue * 100"),
        "net_margin_pct": (("net_profit", "revenue"), "net_profit / revenue * 100"),
        "liabilities_to_equity_pct": (("liabilities", "equity"), "liabilities / equity * 100"),
        "current_ratio_pct": (("current_assets", "current_liabilities"), "current_assets / current_liabilities * 100"),
    }
    calculations = {name: evaluate(names, formula) for name, (names, formula) in definitions.items()}
    calculations["cash_after_asset_acquisitions"] = evaluate(
        ("operating_cash_flow", "ppe_purchases", "intangible_purchases"),
        "operating_cash_flow - abs(ppe_purchases) - abs(intangible_purchases)", cash=True)
    flattened = [c for group in comparisons for c in group]
    for name, field in (("change_amount", "status"), ("change_pct", "pct_status")):
        calculated = sum(c.get(field) == "calculated" for c in flattened)
        calculations[name] = {"status": "calculated" if calculated else "unavailable", "calculated_items": calculated,
                              "unavailable_items": len(flattened)-calculated,
                              "location": "structured_rows.body.calculated.comparisons"}
    result = metrics(payload)
    result["processor_version"] = PROCESSOR_VERSION
    result["calculation_inputs"] = inputs
    result["fixed_calculations"] = calculations
    for name in definitions:
        result["ratios"].pop(name, None)
        if calculations[name]["status"] == "calculated":
            result["ratios"][name] = calculations[name]["value"]
    result["amounts"] = {"cash_after_asset_acquisitions": calculations["cash_after_asset_acquisitions"]["value"]}
    return result
