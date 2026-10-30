"""Messages round-trip through the standard library parser, with the ISO structure intact."""

import xml.etree.ElementTree as ET
from datetime import date

import pytest
from custodian_connector import SettlementInstruction, build_sese020, build_sese023, parse
from custodian_connector.iso import NS, NS_HEAD

LEG = SettlementInstruction(tx_id="TX-1", movement="RECE", tx_type="SECB", isin="XS0000000001", quantity=100000,
                            trade_date=date(2026, 10, 27), settlement_date=date(2026, 10, 28),
                            counterparty_bic="MKHAUS33XXX", our_bic="LDSKUS33XXX", common_id="UTI-1")


def test_sese023_structure():
    root = ET.fromstring(build_sese023(LEG))
    n = {"h": NS_HEAD, "s": NS["sese.023"]}
    assert root.find("h:AppHdr/h:MsgDefIdr", n).text == "sese.023.001.09"
    x = root.find("s:Document/s:SctiesSttlmTxInstr", n)
    assert x.find("s:TxId", n).text == "TX-1"
    assert x.find("s:SttlmTpAndAddtlParams/s:SctiesMvmntTp", n).text == "RECE"
    assert x.find("s:SttlmTpAndAddtlParams/s:CmonId", n).text == "UTI-1"
    assert x.find("s:TradDtls/s:SttlmDt/s:Dt/s:Dt", n).text == "2026-10-28"
    assert x.find("s:DlvrgSttlmPties/s:Pty1/s:Id/s:AnyBIC", n).text == "MKHAUS33XXX"  # they deliver to us
    assert x.find("s:RcvgSttlmPties/s:Pty1/s:Id/s:AnyBIC", n).text == "LDSKUS33XXX"
    assert x.find("s:SttlmAmt", n) is None  # free of payment


def test_against_payment_needs_amount():
    with pytest.raises(ValueError):
        build_sese023(SettlementInstruction(**{**LEG.__dict__, "payment": "APMT"}))
    xml = build_sese023(SettlementInstruction(**{**LEG.__dict__, "payment": "APMT", "amount": 1.5e6, "ccy": "USD"}))
    assert b'Ccy="USD"' in xml and b"<s:CdtDbtInd>DBIT</s:CdtDbtInd>" in xml


def test_sese020_and_status_parsing():
    root = ET.fromstring(build_sese020("TX-1", "LDSKUS33XXX", "CUS-00000001"))
    n = {"s": NS["sese.020"]}
    assert root.find("s:Document/s:SctiesTxCxlReq/s:AcctOwnrTxId", n).text == "TX-1"

    s24 = NS["sese.024"]
    advice = parse(f"""<BizMsg xmlns="urn:loandesk:mock:envelope" xmlns:h="{NS_HEAD}" xmlns:s="{s24}">
      <h:AppHdr><h:BizMsgIdr>CUS-1-F1</h:BizMsgIdr></h:AppHdr>
      <s:Document><s:SctiesSttlmTxStsAdvc><s:TxId><s:AcctOwnrTxId>TX-1</s:AcctOwnrTxId></s:TxId>
        <s:AcctSvcrTxId>CUS-00000001</s:AcctSvcrTxId>
        <s:SttlmSts><s:PdgFlng><s:Rsn><s:Cd><s:Cd>LACK</s:Cd></s:Cd><s:AddtlRsnInf>lack of securities</s:AddtlRsnInf>
        </s:Rsn></s:PdgFlng></s:SttlmSts></s:SctiesSttlmTxStsAdvc></s:Document></BizMsg>""")
    assert (advice.msg_type, advice.status, advice.reason, advice.tx_id, advice.custodian_ref) == (
        "sese.024", "PENF", "LACK", "TX-1", "CUS-00000001")
    assert not advice.is_final
