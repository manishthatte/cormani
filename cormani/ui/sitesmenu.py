# SPDX-License-Identifier: GPL-3.0-or-later
#
# The Panels menu — every site in the registry, with a tick for each one on.
#
# © Manish Jagdish Thatte
from __future__ import annotations


def attach(window, keys, *, user_agent: str = "") -> None:
    from ..panels import sites as sites_mod

    window.mail.sites.set_user_agent(user_agent)
    window._site_keys = [k for k in (keys or []) if sites_mod.get(k) is not None]
    window.mail.rail.set_sites(window._site_keys)
    rebuild(window)


def rebuild(window, keys=None) -> None:
    from ..panels import sites as sites_mod

    menu = window.sites_menu
    menu.clear()
    enabled = set(window._site_keys if keys is None else keys)
    menu.menuAction().setVisible(True)

    for site in sites_mod.SITES:
        action = menu.addAction(site.name)
        action.setCheckable(True)
        action.setChecked(site.key in enabled)
        action.triggered.connect(
            lambda _=False, k=site.key: panel_toggled(window, k))

    if enabled:
        menu.addSeparator()
        open_menu = menu.addMenu("&Open")
        for key in sorted(enabled):
            site = sites_mod.get(key)
            if site is None:
                continue
            action = open_menu.addAction(site.name)
            action.triggered.connect(
                lambda _=False, k=key: window.mail.show_site(k))

        menu.addSeparator()
        out = menu.addMenu("Sign &out of")
        for key in sorted(enabled):
            site = sites_mod.get(key)
            if site is None:
                continue
            action = out.addAction(f"{site.name}…")
            action.triggered.connect(
                lambda _=False, k=key: window.sign_out_of_site(k))


def panel_toggled(window, key: str) -> None:
    from ..panels import sites as sites_mod

    site = sites_mod.get(key)
    if site is None:
        return
    action = next((a for a in window.sites_menu.actions()
                   if a.text() == site.name), None)
    checked = action.isChecked() if action is not None else False
    keys = list(window._site_keys)
    if checked and key not in keys:
        keys.append(key)
    elif not checked and key in keys:
        keys.remove(key)
    attach(window, keys)


def demo_tour(window) -> None:
    window.mail.show_tracking()
    window.status_message.setText(
        "Tour: the Tracking tab shows conversations you owe a reply on, "
        "deadlines, and where each thread stands.")
