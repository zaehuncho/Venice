// [SERVER-SHARD D3] machine_id parity between the OrionActivate broker and
// SecurityManager::machineId().
//
// This test COMPILES MachineIdentity.cpp directly (its own copy of
// orion::deriveMachineId, exactly as the broker exe does) AND links SecurityCore
// (whose SecurityManager::machineId() calls SecurityCore's own compiled copy of
// the same source). Asserting the two are equal proves that the SAME shared
// translation unit, compiled into two independent binaries, yields a byte-
// identical machine_id on this host - which is the broker-vs-app scenario. If the
// derivation ever drifts (e.g. someone re-implements machineId by hand), this
// fails, and every clean install would otherwise fail machine binding.

#include <QtCore/QString>
#include <QtTest/QtTest>

#include "MachineIdentity.h"
#include "SecurityManager.h"

class MachineIdParityTests : public QObject
{
    Q_OBJECT

private slots:
    void deriveMatchesSecurityManager()
    {
        const QString broker = orion::deriveMachineId();
        orion::SecurityManager sm(QStringLiteral("."));
        const QString app = sm.machineId();

        QVERIFY2(!broker.isEmpty(), "broker deriveMachineId() returned empty");
        QVERIFY2(!app.isEmpty(), "SecurityManager::machineId() returned empty");
        QCOMPARE(broker, app);
    }

    void machineIdIsSixtyFourLowercaseHex()
    {
        const QString id = orion::deriveMachineId();
        QCOMPARE(id.size(), 64);
        for (const QChar c : id) {
            const bool hex = (c >= QLatin1Char('0') && c <= QLatin1Char('9'))
                          || (c >= QLatin1Char('a') && c <= QLatin1Char('f'));
            QVERIFY2(hex, "machine_id must be 64 lowercase hex chars (shard_bootstrap.c is_hex_string)");
        }
    }

    void derivationIsStableAcrossCalls()
    {
        // The DPAPI record is written once and re-read on every launch; the id
        // must not wander between calls within a boot.
        QCOMPARE(orion::deriveMachineId(), orion::deriveMachineId());
    }
};

QTEST_GUILESS_MAIN(MachineIdParityTests)
#include "MachineIdParityTests.moc"
