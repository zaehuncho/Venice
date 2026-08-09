// Production fence for the inbound meter delay.
//
// Compiled TWICE by CMake from this one file:
//   OrionMeterDelayFenceDevTests   -- without ORION_PRODUCTION_BUILD
//   OrionMeterDelayFenceProdTests  -- with    ORION_PRODUCTION_BUILD
//
// A compile-time fence that is only ever built one way is a fence nobody has
// tested. Building both proves the guard actually changes behaviour rather than
// merely existing, and that the dev path is not accidentally fenced (which
// would make the feature untestable and look like a broken feature instead of
// an intentional one).

#include "MeterDelayController.h"

#include <QtTest/QTest>

using orion::MeterDelayController;

class MeterDelayProductionFenceTests final : public QObject {
    Q_OBJECT

private slots:
    void fenceMatchesTheBuildFlag();
    void enablingIsRefusedInAProductionBuild();
    void theRefusalSaysWhy();
};

void MeterDelayProductionFenceTests::fenceMatchesTheBuildFlag()
{
#ifdef ORION_PRODUCTION_BUILD
    QVERIFY(MeterDelayController::productionFenced());
#else
    QVERIFY(!MeterDelayController::productionFenced());
#endif
}

void MeterDelayProductionFenceTests::enablingIsRefusedInAProductionBuild()
{
    MeterDelayController c;
    c.setManualTickMode(true);
    QVERIFY(!c.enabled());

    c.setEnabled(true);
    c.setPlayingGame(true);
    c.setCourtIpKnown(true);
    for (int i = 0; i < 40; ++i) c.tick();

#ifdef ORION_PRODUCTION_BUILD
    // The whole point: no amount of correct-looking session state can arm it.
    QVERIFY2(!c.enabled(), "production build allowed the meter delay to enable");
    QCOMPARE(c.sessionState(), MeterDelayController::SessionState::Idle);
    QCOMPARE(c.currentDelayMs(), 0.0);
    QVERIFY(!c.interceptActive());
#else
    // And equally: the dev path must NOT be fenced, or the feature is dead
    // everywhere and no one finds out until they try to test it.
    QVERIFY(c.enabled());
    QVERIFY(c.sessionState() != MeterDelayController::SessionState::Idle);
#endif
}

void MeterDelayProductionFenceTests::theRefusalSaysWhy()
{
    MeterDelayController c;
    c.setManualTickMode(true);
    c.setEnabled(true);
#ifdef ORION_PRODUCTION_BUILD
    QVERIFY2(c.reasonText().contains(QStringLiteral("production build fence")),
             qPrintable(QStringLiteral("silent refusal; reason was: %1")
                            .arg(c.reasonText())));
    QVERIFY(c.reasonText().contains(QStringLiteral("ORION_PRODUCTION_BUILD")));
#else
    QVERIFY(!c.reasonText().contains(QStringLiteral("production build fence")));
#endif
}

QTEST_MAIN(MeterDelayProductionFenceTests)
#include "MeterDelayProductionFenceTests.moc"
