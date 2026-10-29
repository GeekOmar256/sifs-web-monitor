"""Pure control rules, kept separate so they can be tested without Pi hardware."""


def parse_co2_packet(packet):
    if len(packet) != 9 or packet[:2] != bytes.fromhex("FF 86"):
        raise ValueError("invalid CO2 response")
    if ((-sum(packet[1:8])) & 0xFF) != packet[8]:
        raise ValueError("CO2 checksum error")
    return packet[2] * 256 + packet[3]


def pump_should_run(mode, manual_on, soil_state, is_active, started_at,
                    stopped_at, now, max_run=30, min_rest=10):
    if soil_state in (None, "wet"):
        return False
    requested = soil_state == "dry" if mode == "auto" else manual_on is True
    if not requested:
        return False
    if is_active and started_at is not None and now - started_at >= max_run:
        return False
    if not is_active and now - stopped_at < min_rest:
        return False
    return True


def light_should_run(mode, manual_on, co2, was_on, threshold, hysteresis):
    if mode == "manual":
        return manual_on is True
    if co2 is None:
        return False
    if co2 >= threshold:
        return True
    if co2 <= threshold - hysteresis:
        return False
    return was_on
