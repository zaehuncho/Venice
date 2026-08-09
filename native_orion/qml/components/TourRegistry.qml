pragma Singleton
import QtQuick

// Lightweight anchor registry so the first-run coach-mark tour (FirstRunTour.qml)
// can spotlight the REAL controls wherever they live in the tree — a sidebar nav
// item, the Connect button, the meter panel — without those pages knowing anything
// about the tour. Targets self-register by a stable key in Component.onCompleted and
// drop themselves in Component.onDestruction. The overlay resolves a key to a live
// Item, maps its rect into overlay space, and gracefully centers its caption when the
// key is absent/hidden (element not present -> step degrades, never breaks).
QtObject {
    id: reg

    // key(string) -> Item. Kept as a plain JS object (not a QML property map) so
    // per-frame lookups from the overlay's re-measure timer never churn bindings.
    property var _items: ({})

    // Bumped on every register/unregister. Bindings/lookups that touch this re-run
    // when the set of live anchors changes (e.g. a page mounts after the tour opens).
    property int revision: 0

    function register(key, item) {
        if (!key || !item)
            return
        _items[key] = item
        revision += 1
    }

    function unregister(key, item) {
        // Only drop if the stored item is the one unregistering (a late-arriving
        // replacement for the same key must not be clobbered by an old destructor).
        if (key && _items.hasOwnProperty(key) && (item === undefined || _items[key] === item)) {
            delete _items[key]
            revision += 1
        }
    }

    // Live Item for key, or null. Touch `revision` at the call site to re-resolve.
    function item(key) {
        if (!key)
            return null
        var it = _items[key]
        return (it !== undefined && it !== null) ? it : null
    }
}
