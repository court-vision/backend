"""
The scheduled pickup's email (services/notification_service.py) for a pickup the roster lock
changed: the drop was protected after the pickup was scheduled (services/roster_protection.py).
"""

import pytest

from services.notification_service import NotificationService


def result(outcome, reason, **kw):
    base = dict(outcome=outcome, reason=reason, add={"name": "Kawhi Leonard"}, drop={"name": "Anthony Edwards"},
                nba_date="2026-10-22", verified=True, seated_slot="UT")
    base.update(kw)
    return base


@pytest.mark.unit
def test_an_add_only_because_the_drop_is_protected_says_so_and_never_says_he_was_dropped():
    subject, body = NotificationService._build_pickup_email("GloatingSoap369", result("executed", "drop_protected"))
    assert subject == "Court Vision picked up Kawhi Leonard for GloatingSoap369"
    assert "Added Kawhi Leonard on ESPN." in body and "dropped Anthony Edwards" not in body
    assert "Anthony Edwards is protected, so this was an add only." in body


@pytest.mark.unit
def test_a_pickup_skipped_because_the_drop_is_protected_names_why():
    _, body = NotificationService._build_pickup_email("GloatingSoap369", result("skipped", "drop_protected"))
    assert "The pickup was not made: Anthony Edwards is protected and there was no open seat." in body
