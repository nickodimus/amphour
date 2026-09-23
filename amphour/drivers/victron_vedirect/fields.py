"""VE.Direct label map.

Every entry below was cross-checked on 2026-09-09 against the `description`
and `units` an independent implementation (the Node-RED victron-vedirect node)
recorded for the same labels from the same physical device, which is why they
are marked `confirmed` rather than taken on trust from the specification.

Field NAMES are the shared vocabulary described in amphour/fields.py, not the
raw VE.Direct labels: `V` becomes `battery_voltage` because that is what the
Renogy driver calls the same physical quantity, so one query compares them.

Note `battery_current` is deliberately NOT the same field as the Renogy
driver's `battery_charging_current`. A shunt measures net current into and out
of the bank, signed; a charge controller measures only what it is itself
delivering. Same units, different quantities, so different names.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from ...fields import Confidence, Field

Kind = Literal["number", "text"]


@dataclass(frozen=True, slots=True)
class Label:
    """One VE.Direct label and how to interpret its text value.

    `codes` turns a numeric status word into a name. A label with codes is
    reported as TEXT, never as a number, and that is a deliberate choice - see
    the note on CS below.
    """

    label: str
    name: str
    kind: Kind
    scale: float
    unit: str
    confidence: Confidence
    help: str
    codes: Mapping[int, str] | None = None

    def as_field(self) -> Field:
        return Field(self.name, self.unit, self.confidence, self.help)


LABELS: Final[tuple[Label, ...]] = (
    # --- instantaneous ---
    Label("V", "battery_voltage", "number", 0.001, "volts", "confirmed", "Main (battery) voltage"),
    Label(
        "I",
        "battery_current",
        "number",
        0.001,
        "amperes",
        "confirmed",
        "Net battery current, positive charging and negative discharging",
    ),
    Label(
        "P",
        "battery_power",
        "number",
        1,
        "watts",
        "confirmed",
        "Instantaneous power into or out of the battery",
    ),
    Label(
        "CE",
        "consumed_amp_hours",
        "number",
        0.001,
        "amp_hours",
        "confirmed",
        "Charge consumed since the last full charge",
    ),
    Label(
        "SOC",
        "battery_state_of_charge",
        "number",
        0.1,
        "percent",
        "confirmed",
        "State of charge, coulomb-counted rather than derived from voltage",
    ),
    Label(
        "TTG",
        "time_to_go_minutes",
        "number",
        1,
        "minutes",
        "confirmed",
        "Estimated time until the battery is empty; -1 means infinite",
    ),
    Label("AR", "alarm_reason", "number", 1, "bitfield", "confirmed", "Alarm reason bitfield"),
    Label("MON", "dc_monitor_mode", "number", 1, "enum", "confirmed", "DC monitor mode"),
    Label("Alarm", "alarm", "text", 1, "state", "confirmed", "Alarm condition active"),
    Label(
        "BMV",
        "model",
        "text",
        1,
        "text",
        "confirmed",
        "Model description (deprecated by Victron in favour of PID)",
    ),
    Label("FW", "firmware_version", "text", 1, "text", "confirmed", "Firmware version, 16 bit"),
    Label("PID", "product_id", "text", 1, "text", "confirmed", "Product ID"),
    # --- lifetime history ---
    Label(
        "H1",
        "deepest_discharge",
        "number",
        0.001,
        "amp_hours",
        "confirmed",
        "Depth of the deepest discharge",
    ),
    Label(
        "H2",
        "last_discharge",
        "number",
        0.001,
        "amp_hours",
        "confirmed",
        "Depth of the last discharge",
    ),
    Label(
        "H3",
        "average_discharge",
        "number",
        0.001,
        "amp_hours",
        "confirmed",
        "Depth of the average discharge",
    ),
    Label("H4", "charge_cycles", "number", 1, "count", "confirmed", "Number of charge cycles"),
    Label("H5", "full_discharges", "number", 1, "count", "confirmed", "Number of full discharges"),
    Label(
        "H6",
        "cumulative_amp_hours_drawn",
        "number",
        0.001,
        "amp_hours",
        "confirmed",
        "Cumulative amp hours drawn",
    ),
    Label(
        "H7",
        "battery_voltage_min_lifetime",
        "number",
        0.001,
        "volts",
        "confirmed",
        "Minimum main battery voltage ever seen",
    ),
    Label(
        "H8",
        "battery_voltage_max_lifetime",
        "number",
        0.001,
        "volts",
        "confirmed",
        "Maximum main battery voltage ever seen",
    ),
    Label(
        "H9",
        "seconds_since_full_charge",
        "number",
        1,
        "seconds",
        "confirmed",
        "Seconds since the last full charge. A shunt that syncs to 100% on a "
        "worn battery will show a small value here while reporting an "
        "implausibly high state of charge",
    ),
    Label(
        "H10",
        "automatic_syncs",
        "number",
        1,
        "count",
        "confirmed",
        "Number of automatic synchronisations",
    ),
    Label(
        "H11",
        "low_voltage_alarms",
        "number",
        1,
        "count",
        "confirmed",
        "Number of low main voltage alarms",
    ),
    Label(
        "H12",
        "high_voltage_alarms",
        "number",
        1,
        "count",
        "confirmed",
        "Number of high main voltage alarms",
    ),
    Label(
        "H15",
        "aux_voltage_min_lifetime",
        "number",
        0.001,
        "volts",
        "confirmed",
        "Minimum auxiliary battery voltage",
    ),
    Label(
        "H16",
        "aux_voltage_max_lifetime",
        "number",
        0.001,
        "volts",
        "confirmed",
        "Maximum auxiliary battery voltage",
    ),
    Label(
        "H17",
        "discharged_energy",
        "number",
        0.01,
        "kilowatt_hours",
        "confirmed",
        "Cumulative discharged energy",
    ),
    Label(
        "H18",
        "charged_energy",
        "number",
        0.01,
        "kilowatt_hours",
        "confirmed",
        "Cumulative charged energy",
    ),
    # --- solar charge controller (SmartSolar MPPT) ------------------------
    #
    # A VE.Direct MPPT speaks the identical protocol with a different label
    # set, so it needs no separate driver - only this vocabulary. These entries
    # come from the published VE.Direct protocol document and, unlike
    # everything above, have NOT been cross-checked against a second
    # implementation or a live unit. They therefore ship `unverified` and are
    # not exported by default. Promote them once they have been read against
    # VictronConnect on the real controller.
    #
    # Names are chosen to match what the Renogy charge controller already calls
    # the same physical quantity, so one query compares two controllers.
    Label("VPV", "pv_voltage", "number", 0.001, "volts", "unverified", "Panel voltage"),
    Label("PPV", "pv_power", "number", 1, "watts", "unverified", "Panel power"),
    Label(
        "IL",
        "load_current",
        "number",
        0.001,
        "amperes",
        "unverified",
        "Current delivered through the load output terminals",
    ),
    Label("LOAD", "load_state", "text", 1, "state", "unverified", "Load output on or off"),
    # CS is reported as TEXT and deliberately has NO numeric twin.
    #
    # The Renogy controller already publishes a numeric `charging_state`, and
    # its code set is NOT Victron's: Renogy 4 means boost, Victron 4 means
    # absorption. Publishing Victron's number under that name would put two
    # incompatible vocabularies on one metric, and anything decoding it with
    # the Renogy table - Matilda reads this field and says the stage out loud -
    # would confidently report the wrong charger state.
    #
    # Text is never exported to Prometheus and overwrites the numeric field in
    # InfluxDB, so reporting the NAME keeps `amphour_charging_state` a
    # Renogy-only metric and leaves every existing consumer correct. Numeric
    # fault alerting belongs on `error_code` below, which is the better signal
    # anyway: CS only says "fault", ERR says which one.
    Label(
        "CS",
        "charging_state",
        "text",
        1,
        "state",
        "unverified",
        "Charger stage by name. Victron's own vocabulary, which does NOT share "
        "code numbers with the Renogy controller's charging_state",
        codes={
            0: "off",
            2: "fault",
            3: "bulk",
            4: "absorption",
            5: "float",
            6: "storage",
            7: "equalize_manual",
            245: "starting_up",
            247: "equalize_auto",
            252: "external_control",
        },
    ),
    Label(
        "ERR",
        "error_code",
        "number",
        1,
        "enum",
        "unverified",
        "Error code; 0 is no error. This is the alertable fault signal - CS "
        "only says that something is wrong, ERR says what",
    ),
    Label(
        "MPPT",
        "tracker_mode",
        "number",
        1,
        "enum",
        "unverified",
        "Tracker operation mode: 0 off, 1 voltage or current limited, 2 active",
    ),
    # Yield is reported by VE.Direct in 0.01 kWh. It is scaled to WATT hours
    # here, not kilowatt hours, because the Renogy controller already publishes
    # power_generation_today/total in watt_hours and one metric cannot carry
    # two units. The shunt's discharged_energy/charged_energy above keep
    # kilowatt_hours because nothing else declares those names.
    Label(
        "H19",
        "power_generation_total",
        "number",
        10,
        "watt_hours",
        "unverified",
        "Lifetime yield",
    ),
    Label(
        "H20",
        "power_generation_today",
        "number",
        10,
        "watt_hours",
        "unverified",
        "Yield today",
    ),
    Label(
        "H21",
        "charging_power_max_today",
        "number",
        1,
        "watts",
        "unverified",
        "Maximum power today",
    ),
    Label(
        "H22",
        "power_generation_yesterday",
        "number",
        10,
        "watt_hours",
        "unverified",
        "Yield yesterday",
    ),
    Label(
        "H23",
        "charging_power_max_yesterday",
        "number",
        1,
        "watts",
        "unverified",
        "Maximum power yesterday",
    ),
    Label(
        "HSDS",
        "day_sequence_number",
        "number",
        1,
        "count",
        "unverified",
        "Day sequence number, 0 to 364; it wraps and is not a date",
    ),
)

BY_LABEL: Final[dict[str, Label]] = {label.label: label for label in LABELS}
FIELDS: Final[tuple[Field, ...]] = tuple(label.as_field() for label in LABELS)
