"""
alert_config.py
All thresholds, the rule catalogue and the corrective-action playbooks.
Recommendations come from the playbooks below; each cause and action is only
offered when the facts measured for the alert satisfy its condition.
"""
LOCAL_UTC_OFFSET = 2
WORK_START = 8
WORK_END = 18
HISTORY_DAYS = 7
MAX_EVIDENCE_EVENTS = 10

T = dict(
    hvac_temp_warn=29.0, hvac_temp_crit=32.0,
    server_temp_warn=25.0, server_temp_crit=30.0,
    reading_gap_min=20,   # telemetry cadence in the demo seed is 15 min (was 10 for 5-min data)
    bandwidth_warn=900.0, bandwidth_crit_mean=980.0, bandwidth_sustain=3,
    packet_loss_warn=1.0, packet_loss_crit_mean=3.0, packet_loss_sustain=3,
    link_flap_count=3, link_flap_window_min=30,
    door_held_open_s=120,
    denial_warn=3, denial_crit=10, denial_window_min=15,   # warn=3: seed scenario of 3 denied badges in 10 min
    camera_offline_min=15,
    motion_burst_count=5, motion_burst_window_min=30,
    battery_warn=20.0, battery_crit=10.0, battery_cluster_gap_min=60,
    anomaly_z=4.0, anomaly_sustain=3, anomaly_min_baseline=30,
    anomaly_min_dev_temp=1.0, anomaly_min_dev_bw=80.0,
)

RULES = {
    "HVAC_HIGH_TEMP":          {"title": "HVAC temperature above threshold", "method": "THRESHOLD", "playbook": "HVAC_TEMP"},
    "HVAC_TEMP_ANOMALY":       {"title": "HVAC temperature anomaly", "method": "ANOMALY", "playbook": "HVAC_TEMP"},
    "HVAC_FILTER_FAULT":       {"title": "HVAC filter fault reported", "method": "THRESHOLD", "playbook": "HVAC_FILTER"},
    "NET_LINK_DOWN":           {"title": "Network link down", "method": "THRESHOLD", "playbook": "NET_LINK_DOWN"},
    "NET_LINK_FLAPPING":       {"title": "Network link flapping", "method": "THRESHOLD", "playbook": "NET_FLAPPING"},
    "NET_BANDWIDTH_HIGH":      {"title": "Sustained high bandwidth", "method": "THRESHOLD", "playbook": "NET_BANDWIDTH"},
    "NET_PACKET_LOSS":         {"title": "Sustained packet loss", "method": "THRESHOLD", "playbook": "NET_LOSS"},
    "NET_BANDWIDTH_ANOMALY":   {"title": "Network traffic anomaly", "method": "ANOMALY", "playbook": "NET_ANOMALY"},
    "ACC_DOOR_FORCED":         {"title": "Door forced open", "method": "THRESHOLD", "playbook": "DOOR_FORCED"},
    "ACC_DOOR_HELD_OPEN":      {"title": "Door held open too long", "method": "THRESHOLD", "playbook": "DOOR_HELD"},
    "ACC_DENIAL_SPIKE":        {"title": "Burst of denied access attempts", "method": "THRESHOLD", "playbook": "DENIAL"},
    "CCTV_OFFLINE_LONG":       {"title": "Camera offline too long", "method": "THRESHOLD", "playbook": "CAM_OFFLINE"},
    "CCTV_AFTER_HOURS_MOTION": {"title": "After-hours motion burst in sensitive zone", "method": "THRESHOLD", "playbook": "MOTION"},
    "SEC_BLIND_SPOT_ACCESS":   {"title": "Door activity while camera offline", "method": "COMPOUND", "playbook": "BLIND_SPOT"},
    "FIRE_SMOKE":              {"title": "Smoke detected", "method": "THRESHOLD", "playbook": "SMOKE"},
    "FIRE_BATTERY_LOW":        {"title": "Fire panel battery low", "method": "THRESHOLD", "playbook": "BATTERY"},
}

OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
       "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}


def holds(facts, cond):
    """True when every condition in cond is met by the measured facts."""
    for key, want in (cond or {}).items():
        have = facts.get(key)
        if isinstance(want, tuple):
            if have is None or not OPS[want[0]](have, want[1]):
                return False
        elif have != want:
            return False
    return True


def describe(facts, cond):
    """Human-readable list of the facts that justify a cause or action."""
    out = []
    for key, want in (cond or {}).items():
        if isinstance(want, tuple):
            out.append(f"{key} = {facts.get(key)} (required {want[0]} {want[1]})")
        else:
            out.append(f"{key} = {facts.get(key)}")
    return out


def C(cid, text, requires=None):
    return {"id": cid, "text": text, "requires": requires}


def A(aid, priority, text, when=None):
    return {"id": aid, "priority": priority, "text": text, "when": when}


PLAYBOOKS = {
    "HVAC_TEMP": {
        "causes": [
            C("FAN_FAILURE", "The unit reported its fan as OFF while the temperature rose, which points to a fan or motor failure.", {"fan_off": True}),
            C("FILTER_BLOCKAGE", "A filter fault was logged on this unit close to the rise, which points to restricted airflow.", {"filter_fault": True}),
            C("COOLING_CAPACITY_LOSS", "The temperature stayed high over several consecutive readings, which points to lost cooling capacity (compressor, refrigerant or setpoint).", {"sustained": True}),
            C("SENSOR_OR_TRANSIENT", "Only a short or isolated deviation was recorded; a sensor glitch or brief transient is possible.", {"sustained": False}),
        ],
        "actions": [
            A("HVAC_VERIFY", 1, "Confirm the reading on site and check the unit's local controller for active faults."),
            A("HVAC_FAN", 2, "Inspect the fan motor, contactor and supply breaker.", {"fan_off": True}),
            A("HVAC_FILTER", 2, "Inspect the air filter and replace it if it is blocked.", {"filter_fault": True}),
            A("HVAC_COOLING", 3, "Check compressor operation, refrigerant pressure and the setpoint.", {"sustained": True}),
            A("HVAC_IT_NOTIFY", 2, "Notify the IT team and be ready to shed non-critical server load if the temperature keeps rising.", {"zone": "Server Room"}),
            A("HVAC_TICKET", 9, "Open a maintenance ticket if readings do not return to normal after these checks."),
        ],
    },
    "HVAC_FILTER": {
        "causes": [C("CLOGGED_FILTER", "The unit itself reported a filter differential-pressure fault.")],
        "actions": [
            A("FILTER_REPLACE", 1, "Inspect the air filter and replace it if it is clogged."),
            A("FILTER_WATCH", 2, "Watch this unit's temperature readings for a rise after the fault."),
        ],
    },
    "NET_LINK_DOWN": {
        "causes": [
            C("LINK_STILL_DOWN", "No LINK_UP was recorded after the LINK_DOWN, so the link may still be down.", {"open": True}),
            C("TRANSIENT_OUTAGE", "The link recovered after a short downtime, which suggests a transient outage.", {"open": False}),
        ],
        "actions": [
            A("LINK_CHECK_PORT", 1, "Check the port, cable and transceiver on both ends of the link.", {"open": True}),
            A("LINK_COUNTERS", 2, "Review the port error counters and logs for the outage period.", {"open": False}),
            A("LINK_DEPENDENTS", 3, "Verify that systems connected through this switch are reachable."),
        ],
    },
    "NET_FLAPPING": {
        "causes": [C("UNSTABLE_LINK", "Several link-down events occurred within a short period, which points to an unstable cable, transceiver or port.")],
        "actions": [
            A("FLAP_REPLACE", 1, "Replace or reseat the cable and transceiver on the affected port."),
            A("FLAP_PORT", 2, "Check speed/duplex settings and the port error counters."),
            A("FLAP_UPSTREAM", 3, "Check the upstream device for errors on its side of the link."),
        ],
    },
    "NET_BANDWIDTH": {
        "causes": [C("LINK_SATURATION", "Utilisation stayed above the warning level across consecutive readings, which indicates link saturation.")],
        "actions": [
            A("BW_TOP_TALKERS", 1, "Identify the top traffic sources (flow data or switch statistics)."),
            A("BW_BACKUPS", 2, "Check for scheduled backups, large transfers or a broadcast storm."),
            A("BW_LOSS", 2, "Packet loss accompanies the load; check for queue drops on this port.", {"packet_loss_max_pct": (">=", 1.0)}),
            A("BW_CAPACITY", 4, "Consider QoS rules or a capacity upgrade if this recurs."),
        ],
    },
    "NET_LOSS": {
        "causes": [
            C("CONGESTION", "Packet loss appeared together with high traffic, which points to congestion.", {"bw_mean_mbps": (">", 900.0)}),
            C("LINK_QUALITY", "Packet loss appeared without high traffic, which points to a physical-layer problem.", {"bw_mean_mbps": ("<=", 900.0)}),
        ],
        "actions": [
            A("LOSS_CONGESTION", 1, "Check queue drops and reduce load on this link.", {"bw_mean_mbps": (">", 900.0)}),
            A("LOSS_PHYSICAL", 1, "Inspect cabling and transceivers and check CRC/error counters.", {"bw_mean_mbps": ("<=", 900.0)}),
        ],
    },
    "NET_ANOMALY": {
        "causes": [
            C("TRAFFIC_SURGE", "Traffic was far above its normal level for this time of day.", {"direction": "high"}),
            C("TRAFFIC_DROP", "Traffic was far below its normal level for this time of day, which suggests a degraded link, a failed uplink or an application outage.", {"direction": "low"}),
        ],
        "actions": [
            A("ANOM_SURGE", 1, "Identify what is generating the extra traffic.", {"direction": "high"}),
            A("ANOM_DROP", 1, "Check uplink status and the services that normally use this link.", {"direction": "low"}),
        ],
    },
    "DOOR_FORCED": {
        "causes": [C("FORCED_ENTRY", "The door controller reported the door as forced open.")],
        "actions": [
            A("FORCED_DISPATCH", 1, "Dispatch security to the door to verify the situation."),
            A("FORCED_CCTV", 2, "Review CCTV footage covering the time of the event."),
            A("FORCED_HARDWARE", 3, "Inspect the door contact, lock and frame for damage or misalignment."),
        ],
    },
    "DOOR_HELD": {
        "causes": [C("DOOR_OBSTRUCTED", "The door stayed open longer than the allowed time; it may be obstructed or propped open.")],
        "actions": [
            A("HELD_CHECK", 1, "Check the door for obstruction and verify the door closer works."),
            A("HELD_CONTACT", 2, "Contact the last card holder or the zone owner."),
        ],
    },
    "DENIAL": {
        "causes": [
            C("UNAUTHORISED_ATTEMPT", "Most attempts used cards unknown to the system, which points to an unauthorised attempt.", {"unknown_card_share": (">=", 0.5)}),
            C("PERMISSION_PROBLEM", "Most attempts came from known cards without permission for this door, which points to a provisioning problem.", {"permission_share": (">=", 0.5)}),
        ],
        "actions": [
            A("DENIAL_LOG", 1, "Review the door access log and CCTV footage for the period."),
            A("DENIAL_GUARD", 1, "Dispatch security to verify the door, because the attempts were outside working hours.", {"after_hours": True}),
            A("DENIAL_SUSPEND", 2, "Consider suspending the cards involved while this is investigated.", {"distinct_cards": ("<=", 3)}),
            A("DENIAL_PROVISION", 3, "Verify card provisioning for the people concerned.", {"permission_share": (">=", 0.5)}),
        ],
    },
    "CAM_OFFLINE": {
        "causes": [
            C("CAMERA_STILL_OFFLINE", "No CAMERA_ONLINE event followed, so the camera may still be offline.", {"open": True}),
            C("CAMERA_RECOVERED", "The camera came back online after a prolonged outage.", {"open": False}),
        ],
        "actions": [
            A("CAM_POWER", 1, "Check the camera's power/PoE switch port and cable."),
            A("CAM_NVR", 2, "Verify connectivity to the recording server."),
            A("CAM_DISPATCH", 3, "Send a technician to the camera if it is still offline.", {"open": True}),
        ],
    },
    "MOTION": {
        "causes": [C("AFTER_HOURS_ACTIVITY", "Repeated motion was recorded outside working hours in a sensitive zone.")],
        "actions": [
            A("MOTION_REVIEW", 1, "Review the footage for the period."),
            A("MOTION_VERIFY", 2, "Check whether scheduled maintenance explains the activity; if not, notify security."),
        ],
    },
    "BLIND_SPOT": {
        "causes": [C("UNMONITORED_ACCESS", "Door activity occurred outside working hours while the camera covering the zone was offline.", {"after_hours": True})],
        "actions": [
            A("BLIND_LOG", 1, "Verify the listed access events with the card holders concerned."),
            A("BLIND_NOTIFY", 1, "Notify security that the zone was unmonitored during these events."),
            A("BLIND_RESTORE", 2, "Restore the camera and review any local recording."),
        ],
    },
    "SMOKE": {
        "causes": [
            C("ALARM_ACTIVE", "No ALARM_RESET followed the smoke detection, so the alarm may still be active.", {"open": True}),
            C("ALARM_CLEARED", "The panel was reset after the detection.", {"open": False}),
        ],
        "actions": [
            A("SMOKE_VERIFY", 1, "Verify the zone on site before resetting the panel."),
            A("SMOKE_PROCEDURE", 1, "If the smoke is confirmed, follow the site evacuation procedure and call the fire brigade.", {"open": True}),
            A("SMOKE_DETECTOR", 2, "Inspect the detector that raised the alarm and record the finding."),
        ],
    },
    "BATTERY": {
        "causes": [
            C("MAINS_LOSS", "The panel reported mains power as lost while the battery fell.", {"mains_lost": True}),
            C("BATTERY_AGEING", "The battery is low although mains power was reported normal, which points to battery ageing or a charger fault.", {"mains_lost": False}),
        ],
        "actions": [
            A("BAT_MAINS", 1, "Restore mains power to the panel and check its supply breaker.", {"mains_lost": True}),
            A("BAT_CHARGER", 2, "Check the charger and replace the battery.", {"mains_lost": False}),
            A("BAT_URGENT", 1, "Treat as urgent: the battery is close to depletion.", {"min_battery_pct": ("<", 10.0)}),
            A("BAT_CONTRACTOR", 3, "Notify the fire-safety maintenance contractor."),
        ],
    },
}
