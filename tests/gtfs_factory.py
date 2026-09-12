"""Constructeurs de flux GTFS-RT synthétiques (aucun réseau, aucune donnée réelle)."""

from google.transit import gtfs_realtime_pb2 as gtfs


def trip_update_feed(trips, timestamp=1789219000):
    """Construit un FeedMessage TripUpdates.

    trips : liste de dicts
        {id, trip_id, start_date, route_id, direction_id,
         schedule_relationship, stop_times:[{seq, stop_id,
         schedule_relationship, arrival_delay, departure_delay, departure_time}]}
    """
    feed = gtfs.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = timestamp
    for t in trips:
        e = feed.entity.add()
        e.id = t["id"]
        tu = e.trip_update
        tu.trip.trip_id = t["trip_id"]
        tu.trip.start_date = t["start_date"]
        tu.trip.route_id = t["route_id"]
        tu.trip.direction_id = t.get("direction_id", 0)
        tu.trip.schedule_relationship = t.get(
            "schedule_relationship", gtfs.TripDescriptor.SCHEDULED
        )
        for st in t["stop_times"]:
            stu = tu.stop_time_update.add()
            stu.stop_sequence = st["seq"]
            stu.stop_id = st["stop_id"]
            rel = st.get("schedule_relationship", "scheduled")
            stu.schedule_relationship = (
                gtfs.TripUpdate.StopTimeUpdate.SKIPPED
                if rel == "skipped"
                else gtfs.TripUpdate.StopTimeUpdate.SCHEDULED
            )
            if st.get("arrival_delay") is not None:
                stu.arrival.delay = st["arrival_delay"]
            if st.get("departure_delay") is not None:
                stu.departure.delay = st["departure_delay"]
            if st.get("departure_time") is not None:
                stu.departure.time = st["departure_time"]
    return feed


def alert_feed(alerts, timestamp=1789219000):
    """Construit un FeedMessage ServiceAlerts.

    alerts : liste de dicts
        {id, header, description, cause,
         routes:[route_id], periods:[(start, end)]}
    """
    feed = gtfs.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.timestamp = timestamp
    for a in alerts:
        e = feed.entity.add()
        e.id = a["id"]
        al = e.alert
        al.header_text.translation.add(language="fr", text=a["header"])
        al.description_text.translation.add(
            language="fr", text=a.get("description", "")
        )
        al.cause = a.get("cause", gtfs.Alert.Cause.OTHER_CAUSE)
        for route_id in a.get("routes", [""]):
            al.informed_entity.add(route_id=route_id)
        for start, end in a.get("periods", [(0, None)]):
            p = al.active_period.add()
            p.start = start
            if end is not None:
                p.end = end
    return feed