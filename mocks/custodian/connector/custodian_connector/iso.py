"""ISO 20022 messages for the custodian, with the standard library only (see ../../API.md).

Out: sese.023 SecuritiesSettlementTransactionInstruction, sese.020 SecuritiesTransactionCancellationRequest.
In: sese.024 status advice, sese.025 confirmation, sese.027 cancellation request status advice.
Every message is a business message (envelope) with a head.001 Business Application Header.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal

NS_ENV = "urn:loandesk:mock:envelope"
NS_HEAD = "urn:iso:std:iso:20022:tech:xsd:head.001.001.02"
NS = {
    "sese.023": "urn:iso:std:iso:20022:tech:xsd:sese.023.001.09",
    "sese.020": "urn:iso:std:iso:20022:tech:xsd:sese.020.001.07",
    "sese.024": "urn:iso:std:iso:20022:tech:xsd:sese.024.001.10",
    "sese.025": "urn:iso:std:iso:20022:tech:xsd:sese.025.001.09",
    "sese.027": "urn:iso:std:iso:20022:tech:xsd:sese.027.001.05",
}
MSG_DEF = {"sese.023": "sese.023.001.09", "sese.020": "sese.020.001.07"}
CUSTODIAN_BIC = "MKCUUS33XXX"

Movement = Literal["RECE", "DELI"]  # receive / deliver securities
Payment = Literal["FREE", "APMT"]  # free of payment / against payment
TxType = Literal["SECL", "SECB", "SLRE", "SBRE", "TRAD"]  # loan, borrow, loan return, borrow return, trade

# Status advice element -> short status code.
_STATUS = {"AccptdForFrtherPrcg": "PACK", "Rjctd": "RJCT", "Mtchd": "MACH", "Umtchd": "NMAT", "PdgFlng": "PENF",
           "Canc": "CAND", "Sttld": "SETT", "Accptd": "ACCP"}


@dataclass
class SettlementInstruction:
    """One settlement leg: e.g. receiving borrowed shares (RECE, SECB) or delivering them back (DELI, SBRE)."""

    tx_id: str  # your reference (AcctOwnrTxId); unique
    movement: Movement
    tx_type: TxType
    isin: str
    quantity: int
    trade_date: date
    settlement_date: date
    counterparty_bic: str
    our_bic: str
    account: str = "LOANDESK-SAFE-01"
    payment: Payment = "FREE"
    common_id: str | None = None  # e.g. the trade's UTI
    amount: float | None = None  # against payment only
    ccy: str | None = None
    biz_msg_id: str | None = None

    @property
    def delivering_bic(self) -> str:
        return self.our_bic if self.movement == "DELI" else self.counterparty_bic

    @property
    def receiving_bic(self) -> str:
        return self.counterparty_bic if self.movement == "DELI" else self.our_bic


@dataclass
class StatusAdvice:
    """A custodian message about one of your instructions."""

    msg_type: str  # sese.024 | sese.025 | sese.027
    status: str  # PACK RJCT MACH NMAT PENF CAND SETT (sese.024/025); ACCP RJCT (sese.027)
    tx_id: str | None  # your AcctOwnrTxId
    custodian_ref: str | None  # AcctSvcrTxId
    reason: str | None = None  # ISO reason code, e.g. LACK, CLAC, DDAT, NARR
    detail: str | None = None
    settled_quantity: int | None = None
    settled_at: str | None = None
    biz_msg_id: str | None = None
    xml: bytes = field(default=b"", repr=False)

    @property
    def is_final(self) -> bool:
        return self.status in ("SETT", "CAND", "RJCT") and self.msg_type != "sese.027"


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _sub(parent: ET.Element, ns: str, tag: str, text: object | None = None, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, _q(ns, tag), attrs)
    if text is not None:
        el.text = str(text)
    return el


def _envelope(msg: str, sender_bic: str, biz_msg_id: str) -> tuple[ET.Element, ET.Element]:
    ET.register_namespace("", NS_ENV)
    ET.register_namespace("h", NS_HEAD)
    ET.register_namespace("s", NS[msg])
    root = ET.Element(_q(NS_ENV, "BizMsg"))
    hdr = _sub(root, NS_HEAD, "AppHdr")
    for tag, bic in (("Fr", sender_bic), ("To", CUSTODIAN_BIC)):
        _sub(_sub(_sub(_sub(hdr, NS_HEAD, tag), NS_HEAD, "FIId"), NS_HEAD, "FinInstnId"), NS_HEAD, "BICFI", bic)
    _sub(hdr, NS_HEAD, "BizMsgIdr", biz_msg_id[:35])
    _sub(hdr, NS_HEAD, "MsgDefIdr", MSG_DEF[msg])
    _sub(hdr, NS_HEAD, "CreDt", datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"))
    return root, _sub(root, NS[msg], "Document")


def _bytes(root: ET.Element) -> bytes:
    ET.indent(root)
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _date(parent: ET.Element, ns: str, tag: str, d: date) -> None:
    _sub(_sub(_sub(parent, ns, tag), ns, "Dt"), ns, "Dt", d.isoformat())


def build_sese023(i: SettlementInstruction) -> bytes:
    ns = NS["sese.023"]
    root, doc = _envelope("sese.023", i.our_bic, i.biz_msg_id or f"BM-{i.tx_id}")
    x = _sub(doc, ns, "SctiesSttlmTxInstr")
    _sub(x, ns, "TxId", i.tx_id)
    p = _sub(x, ns, "SttlmTpAndAddtlParams")
    _sub(p, ns, "SctiesMvmntTp", i.movement)
    _sub(p, ns, "Pmt", i.payment)
    if i.common_id:
        _sub(p, ns, "CmonId", i.common_id[:35])
    td = _sub(x, ns, "TradDtls")
    _date(td, ns, "TradDt", i.trade_date)
    _date(td, ns, "SttlmDt", i.settlement_date)
    _sub(_sub(x, ns, "FinInstrmId"), ns, "ISIN", i.isin)
    q = _sub(x, ns, "QtyAndAcctDtls")
    _sub(_sub(_sub(q, ns, "SttlmQty"), ns, "Qty"), ns, "Unit", i.quantity)
    _sub(_sub(q, ns, "SfkpgAcct"), ns, "Id", i.account)
    _sub(_sub(_sub(x, ns, "SttlmParams"), ns, "SctiesTxTp"), ns, "Cd", i.tx_type)
    for tag, bic in (("DlvrgSttlmPties", i.delivering_bic), ("RcvgSttlmPties", i.receiving_bic)):
        _sub(_sub(_sub(_sub(x, ns, tag), ns, "Pty1"), ns, "Id"), ns, "AnyBIC", bic)
    if i.payment == "APMT":
        if i.amount is None or not i.ccy:
            raise ValueError("against-payment instructions need amount and ccy")
        a = _sub(x, ns, "SttlmAmt")
        _sub(a, ns, "Amt", f"{i.amount:.2f}", Ccy=i.ccy)
        _sub(a, ns, "CdtDbtInd", "CRDT" if i.movement == "DELI" else "DBIT")
    return _bytes(root)


def build_sese020(tx_id: str, our_bic: str, custodian_ref: str | None = None, biz_msg_id: str | None = None) -> bytes:
    ns = NS["sese.020"]
    root, doc = _envelope("sese.020", our_bic, biz_msg_id or f"CXL-{tx_id}")
    x = _sub(doc, ns, "SctiesTxCxlReq")
    _sub(x, ns, "AcctOwnrTxId", tx_id)
    if custodian_ref:
        _sub(x, ns, "AcctSvcrTxId", custodian_ref)
    return _bytes(root)


def parse(xml: bytes | str) -> StatusAdvice:
    """Parse any custodian message (sese.024, sese.025 or sese.027)."""
    raw = xml.encode() if isinstance(xml, str) else xml
    root = ET.fromstring(raw)
    for msg in ("sese.024", "sese.025", "sese.027"):
        doc = root.find(_q(NS[msg], "Document"))
        if doc is not None:
            break
    else:
        raise ValueError("not a sese.024, sese.025 or sese.027 message")
    ns = NS[msg]

    def text(path: str) -> str | None:
        el = doc.find(path.format(n=f"{{{ns}}}"))
        return el.text.strip() if el is not None and el.text else None

    biz = root.find(f"{_q(NS_HEAD, 'AppHdr')}/{_q(NS_HEAD, 'BizMsgIdr')}")
    advice = StatusAdvice(msg_type=msg, status="", tx_id=None, custodian_ref=None,
                          biz_msg_id=biz.text if biz is not None else None, xml=raw)
    if msg == "sese.025":
        conf = "{n}SctiesSttlmTxConf/"
        advice.status = "SETT"
        advice.tx_id = text(conf + "{n}TxId/{n}AcctOwnrTxId")
        advice.custodian_ref = text(conf + "{n}AcctSvcrTxId")
        qty = text(conf + "{n}QtyAndAcctDtls/{n}SttldQty/{n}Qty/{n}Unit")
        advice.settled_quantity = int(float(qty)) if qty else None
        advice.settled_at = text(conf + "{n}TradDtls/{n}FctvSttlmDt/{n}Dt/{n}DtTm")
        return advice
    groups: tuple[str, ...]
    if msg == "sese.024":
        body = doc.find(_q(ns, "SctiesSttlmTxStsAdvc"))
        advice.tx_id = text("{n}SctiesSttlmTxStsAdvc/{n}TxId/{n}AcctOwnrTxId")
        advice.custodian_ref = text("{n}SctiesSttlmTxStsAdvc/{n}AcctSvcrTxId")
        groups = ("AcptncSts", "MtchgSts", "SttlmSts", "CxlSts")
    else:
        body = doc.find(_q(ns, "SctiesTxCxlReqStsAdvc"))
        advice.tx_id = text("{n}SctiesTxCxlReqStsAdvc/{n}TxId/{n}AcctOwnrTxId")
        advice.custodian_ref = text("{n}SctiesTxCxlReqStsAdvc/{n}TxId/{n}AcctSvcrTxId")
        groups = ("CxlReqSts",)
    assert body is not None
    for g in groups:
        grp = body.find(_q(ns, g))
        if grp is not None and len(grp):
            code_el = grp[0]
            advice.status = _STATUS.get(code_el.tag.split("}")[1], code_el.tag.split("}")[1])
            rsn = code_el.find(_q(ns, "Rsn"))
            if rsn is not None:
                cd = rsn.find(f"{_q(ns, 'Cd')}/{_q(ns, 'Cd')}")
                info = rsn.find(_q(ns, "AddtlRsnInf"))
                advice.reason = cd.text if cd is not None else None
                advice.detail = info.text if info is not None else None
            break
    return advice
