"""Build and parse the ISO 20022 subset the mock custodian speaks (head.001 + sese.023/024/025)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from lxml import etree

from mockcore import clock

XSD_DIR = Path(__file__).parent / "xsd"
NS_ENV = "urn:loandesk:mock:envelope"
NS_HEAD = "urn:iso:std:iso:20022:tech:xsd:head.001.001.02"
NS_023 = "urn:iso:std:iso:20022:tech:xsd:sese.023.001.09"
NS_024 = "urn:iso:std:iso:20022:tech:xsd:sese.024.001.10"
NS_025 = "urn:iso:std:iso:20022:tech:xsd:sese.025.001.09"
CUSTODIAN_BIC = "MKCUUS33XXX"

_schema: etree.XMLSchema | None = None


def schema() -> etree.XMLSchema:
    global _schema
    if _schema is None:
        _schema = etree.XMLSchema(etree.parse(str(XSD_DIR / "envelope.xsd")))
    return _schema


@dataclass
class Instruction:
    biz_msg_id: str
    sender_bic: str
    tx_id: str
    movement: str  # RECE | DELI
    payment: str  # FREE | APMT
    common_id: str | None
    trade_date: date
    settle_date: date
    isin: str
    quantity: int
    account: str
    tx_type: str  # SECL | SECB | SLRE | SBRE | TRAD
    delivering_bic: str
    receiving_bic: str
    amount: float | None
    ccy: str | None
    credit_debit: str | None


class ValidationFailure(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def parse_sese023(xml: bytes) -> Instruction:
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
        doc = etree.fromstring(xml, parser)
    except etree.XMLSyntaxError as e:
        raise ValidationFailure([f"not well-formed XML: {e}"]) from e
    s = schema()
    if not s.validate(doc):
        errors = [f"line {e.line}: {e.message}" for e in s.error_log]  # type: ignore[attr-defined]
        raise ValidationFailure(errors[:10])
    n = {"h": NS_HEAD, "s": NS_023}

    def t(path: str) -> str | None:
        el = doc.find(path, n)
        return el.text.strip() if el is not None and el.text else None

    instr = "s:Document/s:SctiesSttlmTxInstr/"
    amt_el = doc.find(instr + "s:SttlmAmt/s:Amt", n)
    return Instruction(
        biz_msg_id=t("h:AppHdr/h:BizMsgIdr") or "",
        sender_bic=t("h:AppHdr/h:Fr/h:FIId/h:FinInstnId/h:BICFI") or "",
        tx_id=t(instr + "s:TxId") or "",
        movement=t(instr + "s:SttlmTpAndAddtlParams/s:SctiesMvmntTp") or "",
        payment=t(instr + "s:SttlmTpAndAddtlParams/s:Pmt") or "",
        common_id=t(instr + "s:SttlmTpAndAddtlParams/s:CmonId"),
        trade_date=date.fromisoformat(t(instr + "s:TradDtls/s:TradDt/s:Dt/s:Dt") or ""),
        settle_date=date.fromisoformat(t(instr + "s:TradDtls/s:SttlmDt/s:Dt/s:Dt") or ""),
        isin=t(instr + "s:FinInstrmId/s:ISIN") or "",
        quantity=int(float(t(instr + "s:QtyAndAcctDtls/s:SttlmQty/s:Qty/s:Unit") or 0)),
        account=t(instr + "s:QtyAndAcctDtls/s:SfkpgAcct/s:Id") or "",
        tx_type=t(instr + "s:SttlmParams/s:SctiesTxTp/s:Cd") or "",
        delivering_bic=t(instr + "s:DlvrgSttlmPties/s:Pty1/s:Id/s:AnyBIC") or "",
        receiving_bic=t(instr + "s:RcvgSttlmPties/s:Pty1/s:Id/s:AnyBIC") or "",
        amount=float(amt_el.text) if amt_el is not None else None,
        ccy=amt_el.get("Ccy") if amt_el is not None else None,
        credit_debit=t(instr + "s:SttlmAmt/s:CdtDbtInd"),
    )


def _sub(parent: etree._Element, ns: str, tag: str, text: Any = None, **attrs: str) -> etree._Element:
    el = etree.SubElement(parent, f"{{{ns}}}{tag}", **attrs)
    if text is not None:
        el.text = str(text)
    return el


def _app_hdr(root: etree._Element, frm: str, to: str, biz_id: str, msg_def: str) -> None:
    h = _sub(root, NS_HEAD, "AppHdr")
    for tag, bic in (("Fr", frm), ("To", to)):
        p = _sub(h, NS_HEAD, tag)
        _sub(_sub(_sub(p, NS_HEAD, "FIId"), NS_HEAD, "FinInstnId"), NS_HEAD, "BICFI", bic)
    _sub(h, NS_HEAD, "BizMsgIdr", biz_id[:35])
    _sub(h, NS_HEAD, "MsgDefIdr", msg_def)
    _sub(h, NS_HEAD, "CreDt", clock.iso(clock.now()))


def _date_choice(parent: etree._Element, ns: str, tag: str, d: date) -> None:
    _sub(_sub(_sub(parent, ns, tag), ns, "Dt"), ns, "Dt", d.isoformat())


def build_sese023(i: Instruction) -> bytes:
    """Used by the JSON convenience endpoint and by tests/clients as a reference builder."""
    root = etree.Element(f"{{{NS_ENV}}}BizMsg", nsmap={None: NS_ENV, "h": NS_HEAD, "s": NS_023})
    _app_hdr(root, i.sender_bic, CUSTODIAN_BIC, i.biz_msg_id, "sese.023.001.09")
    d = _sub(root, NS_023, "Document")
    x = _sub(d, NS_023, "SctiesSttlmTxInstr")
    _sub(x, NS_023, "TxId", i.tx_id)
    p = _sub(x, NS_023, "SttlmTpAndAddtlParams")
    _sub(p, NS_023, "SctiesMvmntTp", i.movement)
    _sub(p, NS_023, "Pmt", i.payment)
    if i.common_id:
        _sub(p, NS_023, "CmonId", i.common_id)
    td = _sub(x, NS_023, "TradDtls")
    _date_choice(td, NS_023, "TradDt", i.trade_date)
    _date_choice(td, NS_023, "SttlmDt", i.settle_date)
    _sub(_sub(x, NS_023, "FinInstrmId"), NS_023, "ISIN", i.isin)
    q = _sub(x, NS_023, "QtyAndAcctDtls")
    _sub(_sub(_sub(q, NS_023, "SttlmQty"), NS_023, "Qty"), NS_023, "Unit", i.quantity)
    _sub(_sub(q, NS_023, "SfkpgAcct"), NS_023, "Id", i.account)
    _sub(_sub(_sub(x, NS_023, "SttlmParams"), NS_023, "SctiesTxTp"), NS_023, "Cd", i.tx_type)
    for tag, bic in (("DlvrgSttlmPties", i.delivering_bic), ("RcvgSttlmPties", i.receiving_bic)):
        _sub(_sub(_sub(_sub(x, NS_023, tag), NS_023, "Pty1"), NS_023, "Id"), NS_023, "AnyBIC", bic)
    if i.amount is not None and i.ccy:
        a = _sub(x, NS_023, "SttlmAmt")
        _sub(a, NS_023, "Amt", f"{i.amount:.2f}", Ccy=i.ccy)
        _sub(a, NS_023, "CdtDbtInd", i.credit_debit or "DBIT")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def build_sese024(to_bic: str, biz_id: str, tx_id: str, acct_ref: str, status: str, reason: str | None = None,
                  detail: str | None = None) -> bytes:
    """Status advice. ``status`` is one of PACK, RJCT, MACH, NMAT, PENF (pending failing), CAND, SETT."""
    root = etree.Element(f"{{{NS_ENV}}}BizMsg", nsmap={None: NS_ENV, "h": NS_HEAD, "s24": NS_024})
    _app_hdr(root, CUSTODIAN_BIC, to_bic, biz_id, "sese.024.001.10")
    d = _sub(root, NS_024, "Document")
    x = _sub(d, NS_024, "SctiesSttlmTxStsAdvc")
    _sub(_sub(x, NS_024, "TxId"), NS_024, "AcctOwnrTxId", tx_id)
    _sub(x, NS_024, "AcctSvcrTxId", acct_ref)
    group = {"PACK": "AcptncSts", "RJCT": "AcptncSts", "MACH": "MtchgSts", "NMAT": "MtchgSts",
             "PENF": "SttlmSts", "CAND": "CxlSts", "SETT": "SttlmSts"}[status]
    g = _sub(x, NS_024, group)
    code = {"PACK": "AccptdForFrtherPrcg", "RJCT": "Rjctd", "MACH": "Mtchd", "NMAT": "Umtchd",
            "PENF": "PdgFlng", "CAND": "Canc", "SETT": "Sttld"}[status]
    el = _sub(g, NS_024, code)
    if reason:
        rsn = _sub(el, NS_024, "Rsn")
        _sub(_sub(rsn, NS_024, "Cd"), NS_024, "Cd", reason)
        if detail:
            _sub(rsn, NS_024, "AddtlRsnInf", detail[:210])
    else:
        _sub(el, NS_024, "NoSpcfdRsn", "NORE")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def build_sese025(to_bic: str, biz_id: str, i: dict[str, Any], settled_at: datetime) -> bytes:
    root = etree.Element(f"{{{NS_ENV}}}BizMsg", nsmap={None: NS_ENV, "h": NS_HEAD, "s25": NS_025})
    _app_hdr(root, CUSTODIAN_BIC, to_bic, biz_id, "sese.025.001.09")
    d = _sub(root, NS_025, "Document")
    x = _sub(d, NS_025, "SctiesSttlmTxConf")
    _sub(_sub(x, NS_025, "TxId"), NS_025, "AcctOwnrTxId", i["txId"])
    _sub(x, NS_025, "AcctSvcrTxId", i["instructionId"])
    td = _sub(x, NS_025, "TradDtls")
    _date_choice(td, NS_025, "TradDt", date.fromisoformat(i["tradeDate"]))
    _sub(_sub(_sub(td, NS_025, "FctvSttlmDt"), NS_025, "Dt"), NS_025, "DtTm", clock.iso(settled_at))
    _sub(_sub(x, NS_025, "FinInstrmId"), NS_025, "ISIN", i["isin"])
    q = _sub(x, NS_025, "QtyAndAcctDtls")
    _sub(_sub(_sub(q, NS_025, "SttldQty"), NS_025, "Qty"), NS_025, "Unit", i["quantity"])
    _sub(_sub(q, NS_025, "SfkpgAcct"), NS_025, "Id", i["account"])
    _sub(_sub(_sub(x, NS_025, "SttlmParams"), NS_025, "SctiesTxTp"), NS_025, "Cd", i["txType"])
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
