"""
tests/test_ontology.py  (Milestone 1)
Guards the ontology contract: every concept round-trips, validators hold,
deserialisation is hardened, and messages built by the helpers parse back.

Rule: any change to common/ontology.py must be reflected here.
"""
import json
import logging

import pytest

from common.ontology import (
    ONTOLOGY, Perf, Protocol, serialize, deserialize, from_dict,
    create_message, parse_message, parse_payload,
    MicrohubNode, StagingPoint, TripRequest, Pool, Policy, DriverStatus,
    ReputationScore, PoolProposal, CFP, Bid, MatchResult, AuctionOutcome,
    DriverArriving, RiderReady, RiderBoarded, RiderDroppedOff, TripCompleted,
    TripFailed, RepositionRequest, EscrowRequest, Escrow, Settlement, Refund,
    Rating, TrafficUpdate, Disruption, AuditReport, Penalty, OverflowRequest,
    DemandForecast, EscrowStatus, DriverState, PoolStatus, DisruptionType,
    FailureReason, Ping, Pong,
)
from common import messages as m


# ------------------------------------------------------------------
# Sample instances: one per registered concept
# ------------------------------------------------------------------

def _request(rid="req1", rider="rider1"):
    return TripRequest(rid, rider, "hubA", "home1", 0.0, 600.0, 180.0, 12.0, True, False, 5.0)


def _bid(**kw):
    base = dict(bid_id="b1", driver_id="d1", pool_id="p1", price=8.5, eta=120.0,
                max_detour=120.0, capacity_available=3)
    base.update(kw)
    return Bid(**base)


SAMPLES = {
    "MicrohubNode": MicrohubNode("hubA", 5.0, 10),
    "StagingPoint": StagingPoint("sp1", "nodeX", 4, ["hubA", "hubB"], 1),
    "TripRequest": _request(),
    "Pool": Pool("p1", "hubA", [_request("r1", "u1"), _request("r2", "u2")], PoolStatus.LOCKED, 3.0),
    "Policy": Policy("pol1", 1, 0.0, {"base": 2.0, "max": 25.0}, ["zoneN"],
                     {"eligible_discount": 0.5}, {"zoneN": 0.2}),
    "DriverStatus": DriverStatus("d1", "hubA", DriverState.IDLE, 4, 0, 3600.0, 2.0),
    "ReputationScore": ReputationScore("d1", 4.5, 12, 10.0),
    "PoolProposal": PoolProposal("p1", "req1", ["hubA", "home1"], 4.25, 90.0, 0.3),
    "CFP": CFP("p1", "hubA", "hubA", ["home1", "home2"], {"req1": [0.0, 600.0]}, 2, 30.0, 20.0, 1.5, 0,
               {"min_rep": 3.0}),
    "Bid": _bid(reputation=4.2, attempt=1),
    "MatchResult": MatchResult("p1", "d1", 10.0, 120.0, {"u1": 4.0, "u2": 6.0}),
    "AuctionOutcome": AuctionOutcome("p1", False, None, FailureReason.NO_BIDS),
    "DriverArriving": DriverArriving("req1", "p1", "d1", 45.0),
    "RiderReady": RiderReady("req1", "p1", 50.0),
    "RiderBoarded": RiderBoarded("req1", "p1", "d1", 55.0),
    "RiderDroppedOff": RiderDroppedOff("req1", "p1", "d1", 300.0),
    "TripCompleted": TripCompleted("t1", "p1", "d1", ["u1", "u2"], 320.0, 7.5),
    "TripFailed": TripFailed("p1", FailureReason.DRIVER_FAILED, "d1", ["e1", "e2"]),
    "RepositionRequest": RepositionRequest("hubA", "d1", "sp1", 1.5),
    "EscrowRequest": EscrowRequest("req1", "u1", "p1", 6.0, True),
    "Escrow": Escrow("e1", "u1", 6.0, EscrowStatus.HELD, "p1"),
    "Settlement": Settlement("e1", "d1", 10.0, 2.0, 0.0),
    "Refund": Refund("e1", "u1", 6.0, FailureReason.RIDER_CANCELLED),
    "Rating": Rating("t1", "u1", "d1", 5, "great"),
    "TrafficUpdate": TrafficUpdate("tu1", 60.0, {"a|b": 30.0, "b|a": 31.0}),
    "Disruption": Disruption("dis1", DisruptionType.ROAD_CLOSURE, ["a|b"], 100.0, 400.0, 1.0, True),
    "AuditReport": AuditReport("hubA", 0.0, 3600.0, {"zoneN": 0.25}, 0, True),
    "Penalty": Penalty("hubA", "fare cap violation", 50.0),
    "OverflowRequest": OverflowRequest("hubA", FailureReason.HUB_FAILED, [_request()]),
    "DemandForecast": DemandForecast("hubA", 0.0, 900.0, 12.5),
    "Ping": Ping("ping-1", 10.0),
    "Pong": Pong("ping-1", "hub_trindade@localhost", "microhub", 10.0, 10.2),
}


def test_every_registered_concept_has_a_sample():
    """Adding a concept without a sample here fails the build (prevents drift)."""
    assert set(SAMPLES) == set(ONTOLOGY)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_roundtrip(name):
    obj = SAMPLES[name]
    out = deserialize(serialize(obj), type(obj), strict=True)
    assert out == obj
    assert type(out) is ONTOLOGY[name]


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_serialize_is_deterministic_valid_json(name):
    obj = SAMPLES[name]
    s = serialize(obj)
    assert s == serialize(obj)
    json.loads(s)                                   # valid JSON
    assert s == json.dumps(json.loads(s), sort_keys=True)  # keys sorted


# ------------------------------------------------------------------
# Nesting and enums
# ------------------------------------------------------------------

def test_nested_dataclasses_are_rebuilt():
    out = deserialize(serialize(SAMPLES["Pool"]), Pool)
    assert all(isinstance(r, TripRequest) for r in out.requests)
    assert out.size == 2
    out = deserialize(serialize(SAMPLES["OverflowRequest"]), OverflowRequest)
    assert isinstance(out.pending_requests[0], TripRequest)


def test_enums_serialise_as_plain_values_and_restore():
    d = json.loads(serialize(SAMPLES["Escrow"]))
    assert d["status"] == "HELD"
    assert deserialize(serialize(SAMPLES["Escrow"]), Escrow).status is EscrowStatus.HELD


def test_invalid_enum_value_rejected():
    with pytest.raises(ValueError):
        from_dict(Escrow, {"escrow_id": "e", "rider_id": "u", "amount": 1.0, "status": "BOGUS"})


# ------------------------------------------------------------------
# Hardened deserialisation
# ------------------------------------------------------------------

BID_JSON = {"bid_id": "b", "driver_id": "d", "pool_id": "p", "price": 1, "eta": 2,
            "max_detour": 0, "capacity_available": 2}


def test_unknown_field_strict_raises():
    with pytest.raises(ValueError, match="Unknown fields"):
        from_dict(Bid, {**BID_JSON, "surprise": 1}, strict=True)


def test_unknown_field_lenient_warns_and_drops(caplog):
    with caplog.at_level(logging.WARNING):
        bid = from_dict(Bid, {**BID_JSON, "surprise": 1})
    assert not hasattr(bid, "surprise")
    assert "surprise" in caplog.text


def test_strict_propagates_into_nested_objects():
    d = json.loads(serialize(SAMPLES["Pool"]))
    d["requests"][0]["extra"] = 1
    with pytest.raises(ValueError, match="Unknown fields"):
        from_dict(Pool, d, strict=True)
    from_dict(Pool, d)  # lenient is fine


def test_null_for_non_optional_rejected():
    with pytest.raises(ValueError, match="non-Optional"):
        from_dict(Pool, {"pool_id": "p", "hub_id": "h", "requests": [], "status": None})


def test_null_for_optional_accepted():
    bid = from_dict(Bid, {**BID_JSON, "reputation": None})
    assert bid.reputation is None


def test_int_coerced_to_float():
    bid = from_dict(Bid, BID_JSON)
    assert isinstance(bid.price, float) and isinstance(bid.eta, float)


def test_bool_not_accepted_for_int_or_float():
    with pytest.raises(ValueError, match="bool"):
        from_dict(Bid, {**BID_JSON, "capacity_available": True})
    with pytest.raises(ValueError, match="bool"):
        from_dict(Bid, {**BID_JSON, "price": True})


def test_missing_required_field_raises():
    d = dict(BID_JSON)
    del d["price"]
    with pytest.raises(TypeError):
        from_dict(Bid, d)


# ------------------------------------------------------------------
# Validators (__post_init__ also runs on deserialisation)
# ------------------------------------------------------------------

def test_rating_bounds_and_self_rating():
    for bad in (0, 6):
        with pytest.raises(ValueError):
            Rating("t", "a", "b", bad)
    with pytest.raises(ValueError, match="self-rating"):
        Rating("t", "a", "a", 3)
    for ok in (1, 5):
        Rating("t", "a", "b", ok)


def test_reputation_score_bounds():
    with pytest.raises(ValueError):
        ReputationScore("a", 5.5, 1)
    with pytest.raises(ValueError):
        ReputationScore("a", 0.5, 1)
    with pytest.raises(ValueError):
        ReputationScore("a", 3.0, -1)


def test_trip_request_validation():
    with pytest.raises(ValueError):
        TripRequest("r", "u", "o", "h", 10.0, 5.0, 1.0, 5.0, True, False)
    with pytest.raises(ValueError):
        TripRequest("r", "u", "o", "h", 0.0, 5.0, 1.0, -1.0, True, False)


def test_bid_negative_price_rejected():
    with pytest.raises(ValueError):
        _bid(price=-1.0)


def test_driver_status_capacity_invariant():
    with pytest.raises(ValueError):
        DriverStatus("d", "n", DriverState.ON_TRIP, 4, 5, 100.0, 1.0)  # GC: passengers <= capacity
    DriverStatus("d", "n", DriverState.ON_TRIP, 4, 4, 100.0, 1.0)


def test_match_result_fare_invariant():
    with pytest.raises(ValueError, match="sum"):
        MatchResult("p", "d", 10.0, 1.0, {"a": 4.0, "b": 4.0})
    MatchResult("p", "d", 10.0, 1.0, {"a": 4.0, "b": 6.0})
    MatchResult("p", "d", 10.0, 1.0)  # no split given -> not checked


def test_invalid_payload_rejected_on_deserialisation():
    with pytest.raises(ValueError):
        deserialize(json.dumps({"trip_id": "t", "rater_id": "a", "ratee_id": "a", "score": 3}), Rating)


def test_pool_size_property():
    assert SAMPLES["Pool"].size == 2


# ------------------------------------------------------------------
# Message helpers (ontology.create_message / parse_*)
# ------------------------------------------------------------------

def test_create_and_parse_message_metadata():
    msg = create_message("hub@x", Perf.PROPOSE, Protocol.MATCH_AUCTION, "Bid", _bid(), thread="p1")
    perf, proto, onto, body = parse_message(msg)
    assert (perf, proto, onto) == (Perf.PROPOSE, Protocol.MATCH_AUCTION, "Bid")
    assert msg.thread == "p1"
    assert json.loads(body)["bid_id"] == "b1"


def test_parse_payload_rebuilds_the_right_class():
    msg = create_message("x@y", Perf.INFORM, Protocol.ESCROW, "Escrow", SAMPLES["Escrow"])
    assert parse_payload(msg) == SAMPLES["Escrow"]


def test_parse_payload_none_when_no_body_or_concept():
    assert parse_payload(create_message("x@y", Perf.REFUSE, Protocol.MATCH_AUCTION, "", None)) is None


def test_parse_payload_unknown_concept_raises():
    msg = create_message("x@y", Perf.INFORM, Protocol.ESCROW, "Nonsense", "{}")
    with pytest.raises(ValueError, match="Unknown ontology"):
        parse_payload(msg)


def test_parse_payload_strict_flag():
    msg = create_message("x@y", Perf.PROPOSE, Protocol.MATCH_AUCTION, "Bid",
                         json.dumps({**BID_JSON, "extra": 1}))
    parse_payload(msg)
    with pytest.raises(ValueError):
        parse_payload(msg, strict=True)


# ------------------------------------------------------------------
# messages.py helpers  (Milestone 1 done-when: make_cfp -> parse back to CFP)
# ------------------------------------------------------------------

def test_make_cfp_parses_back_into_cfp():
    cfp = SAMPLES["CFP"]
    msg = m.make_cfp("driver@x", cfp)
    assert msg.get_metadata("performative") == Perf.CFP
    assert msg.get_metadata("protocol") == Protocol.MATCH_AUCTION
    assert msg.get_metadata("ontology") == "CFP"
    assert msg.thread == cfp.pool_id
    assert str(msg.to) == "driver@x"
    assert parse_payload(msg, strict=True) == cfp


def test_auction_helpers_use_pool_id_as_thread():
    bid = _bid()
    assert m.make_propose("hub@x", bid).thread == "p1"
    assert parse_payload(m.make_propose("hub@x", bid)) == bid
    match = SAMPLES["MatchResult"]
    acc = m.make_accept_proposal("d@x", match)
    assert acc.get_metadata("performative") == Perf.ACCEPT and acc.thread == match.pool_id
    assert parse_payload(acc) == match
    out = AuctionOutcome("p1", False, "d1", FailureReason.OUTBID)
    rej = m.make_reject_proposal("d@x", out)
    assert rej.get_metadata("performative") == Perf.REJECT
    assert parse_payload(rej) == out


def test_refuse_carries_plain_text_reason():
    msg = m.make_refuse("hub@x", "p1", "locked")
    assert msg.get_metadata("performative") == Perf.REFUSE
    assert msg.body == "locked" and msg.thread == "p1"
    assert parse_payload(msg) is None
    assert m.make_refuse("hub@x", "p1").body is None


def test_auction_failed_message():
    msg = m.make_auction_failed("rider@x", "p1")
    out = parse_payload(msg)
    assert isinstance(out, AuctionOutcome) and not out.awarded and out.reason is FailureReason.NO_BIDS


def test_pool_proposal_helper():
    prop = SAMPLES["PoolProposal"]
    msg = m.make_pool_proposal("rider@x", prop)
    assert msg.get_metadata("protocol") == Protocol.POOL_FORMATION
    assert parse_payload(msg) == prop


def test_generic_inform_and_request_infer_ontology():
    msg = m.make_inform("pay@x", Protocol.SETTLEMENT, SAMPLES["Settlement"], thread="p1")
    assert msg.get_metadata("ontology") == "Settlement"
    assert parse_payload(msg) == SAMPLES["Settlement"]
    assert m.make_request("pay@x", Protocol.ESCROW, SAMPLES["EscrowRequest"]) \
        .get_metadata("performative") == Perf.REQUEST
    assert m.make_query_if("rep@x", Protocol.REPUTATION).body is None


def test_make_message_rejects_unregistered_payload():
    class NotAConcept:
        pass
    with pytest.raises(ValueError, match="not a registered"):
        m.make_message("x@y", Perf.INFORM, Protocol.ESCROW, NotAConcept())


def test_make_reply_swaps_parties_keeps_thread_and_protocol():
    req = m.make_cfp("driver@x", SAMPLES["CFP"])
    req.sender = "hub@x"
    reply = m.make_reply(req, Perf.PROPOSE, _bid())
    assert str(reply.to) == "hub@x"
    assert reply.thread == req.thread
    assert reply.get_metadata("protocol") == Protocol.MATCH_AUCTION
    assert reply.get_metadata("performative") == Perf.PROPOSE
    assert reply.get_metadata("ontology") == "Bid"          # not copied from the CFP
    assert parse_payload(reply) == _bid()


def test_make_reply_requires_sender():
    with pytest.raises(ValueError):
        m.make_reply(m.make_cfp("driver@x", SAMPLES["CFP"]), Perf.REFUSE)


def test_template_matches_only_intended_messages():
    tpl = m.make_template(protocol=Protocol.MATCH_AUCTION, performative=Perf.PROPOSE, thread="p1")
    assert tpl.match(m.make_propose("hub@x", _bid(pool_id="p1")))
    assert not tpl.match(m.make_propose("hub@x", _bid(pool_id="p2")))            # other auction
    assert not tpl.match(m.make_refuse("hub@x", "p1", "locked"))                 # other performative
    assert not tpl.match(m.make_inform("x@y", Protocol.ESCROW, SAMPLES["Escrow"], "p1"))


def test_traffic_update_delta_roundtrip():
    full = SAMPLES["TrafficUpdate"]
    assert full.full_snapshot is True                         # default: backwards compatible
    delta = TrafficUpdate("tu2", 120.0, {"a|b": 33.0}, full_snapshot=False)
    out = deserialize(serialize(delta), TrafficUpdate, strict=True)
    assert out == delta and out.full_snapshot is False
    empty = TrafficUpdate("tu3", 180.0, {}, full_snapshot=False)
    assert deserialize(serialize(empty), TrafficUpdate) == empty


def test_ping_pong_helpers_thread_and_reply():
    ping = Ping("ping-7", 3.0)
    req = m.make_ping("hub@x", ping)
    assert req.get_metadata("performative") == Perf.REQUEST
    assert req.get_metadata("protocol") == Protocol.HEARTBEAT
    assert req.thread == "ping-7" and parse_payload(req, strict=True) == ping
    req.sender = "tester@x"
    pong = Pong("ping-7", "hub@x", "microhub", 3.0, 3.1)
    rep = m.make_pong(req, pong)
    assert str(rep.to) == "tester@x" and rep.thread == "ping-7"
    assert rep.get_metadata("performative") == Perf.INFORM
    assert rep.get_metadata("protocol") == Protocol.HEARTBEAT
    assert parse_payload(rep, strict=True) == pong
