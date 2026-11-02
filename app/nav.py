"""The officer dashboard's navigation, in one table: every page's place, label and icon.

The primary destinations fill the phone's bottom navigation bar and the tablet's navigation rail
(Material Design 3 allows at most five there, so the fifth slot is "More"); the rest open from
"More", and a wide screen's navigation drawer lists them all. Adding a page is one row here.

Simple mode (app.modes) shows only what an officer needs; a `technical` row appears in
Technical mode alone, and `technical_label` renames a destination there."""

from typing import NamedTuple

NAV_BAR_MAX = 5  # Material 3: a navigation bar holds three to five destinations


class NavItem(NamedTuple):
    href: str
    label: str
    icon: str  # a symbol id in the static icon sprite (app/static/vendor/material-symbols/)
    primary: bool = True  # in the bottom bar and the rail; otherwise under "More"
    technical: bool = False  # only in Technical mode
    technical_label: str | None = None  # its name in Technical mode, when that differs
    badge: str | None = None  # a count shown on it (a key of the page's nav counts)

    def label_for(self, technical: bool) -> str:
        return (self.technical_label or self.label) if technical else self.label


NAV: tuple[NavItem, ...] = (
    NavItem("/", "Home", "home", technical_label="Overview"),
    NavItem("/review", "Review", "fact_check", badge="review"),
    NavItem("/incidents", "Incidents", "flood"),
    NavItem("/drafts", "Drafts", "description"),
    NavItem("/reports", "Reports", "forum", primary=False),
    NavItem("/map", "Map", "map", primary=False),
    NavItem("/damaged-houses", "Damaged houses", "house", primary=False),
    NavItem("/accuracy", "Accuracy", "query_stats", primary=False, technical=True),
    NavItem("/help", "Help", "help", primary=False),
)
PRIMARY = tuple(item for item in NAV if item.primary)
SECONDARY = tuple(item for item in NAV if not item.primary)


def destinations(technical: bool) -> tuple[tuple[NavItem, ...], tuple[NavItem, ...]]:
    """The primary destinations and the ones under "More" that this mode shows."""
    shown = tuple(item for item in NAV if technical or not item.technical)
    return (
        tuple(item for item in shown if item.primary),
        tuple(item for item in shown if not item.primary),
    )


def technical_only(path: str) -> bool:
    """The page belongs to Technical mode alone (Simple mode offers to switch instead)."""
    return any(item.technical and nav_current(path, item.href) for item in NAV)


def nav_current(path: str, href: str) -> bool:
    """Current for its own path and the paths below it: /incidents/7 is under Incidents."""
    return path == href or (href != "/" and path.startswith(href + "/"))


def more_current(path: str) -> bool:
    """The page is one of the destinations under "More", so "More" shows as the current section."""
    return any(nav_current(path, item.href) for item in SECONDARY)
