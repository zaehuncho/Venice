// [SERVER-SHARD blocker #5, Codex finding #1] Issuer-SPKI pinning for the
// OrionActivate broker's WinHTTP activation call.
//
//  1. spkiSha256Hex matches Qt's QCryptographicHash SHA-256 for the same bytes
//     (proves the WinHTTP/BCrypt hash is the same digest LicenseClient pins).
//  2. Membership accept/reject against fixtures, incl. fail-closed on empty.
//  3. chainMatchesPins end-to-end accept/reject with an INJECTED pin set (so the
//     accept path is deterministic without needing the live Cloudflare cert).
//  4. Parity: the broker's production pin set is byte-identical (same order) to
//     NetworkSecurity's productionApiPinnedSpkiSha256() — one shared source, no
//     invented pin.

#include <QtCore/QCryptographicHash>
#include <QtCore/QByteArray>
#include <QtCore/QStringList>
#include <QtTest/QtTest>

#include <string>
#include <vector>

#include "SpkiPin.h"
#include "NetworkSecurity.h"

using namespace orion::broker;

class BrokerSpkiPinTests : public QObject
{
    Q_OBJECT

private slots:
    void sha256MatchesQtForFixture()
    {
        const QByteArray fixture = QByteArrayLiteral(
            "\x30\x59\x30\x13\x06\x07\x2a\x86\x48\xce\x3d\x02\x01"
            "SPKI-DER-FIXTURE-not-a-real-key-0123456789");
        const QString qtHex = QString::fromLatin1(
            QCryptographicHash::hash(fixture, QCryptographicHash::Sha256).toHex()).toLower();
        const std::string got = spkiSha256Hex(fixture.constData(),
                                              static_cast<std::size_t>(fixture.size()));
        QCOMPARE(QString::fromStdString(got), qtHex);
        QCOMPARE(got.size(), std::size_t(64));
    }

    void sha256EmptyInputFailsClosed()
    {
        QVERIFY(spkiSha256Hex(nullptr, 0).empty());
        const char x = 'x';
        QVERIFY(spkiSha256Hex(&x, 0).empty());
    }

    void membershipAcceptsRealPinRejectsOthers()
    {
        // GTS WE1 primary pin — the live issuer.
        QVERIFY(isPinnedSpkiHex(
            "908769e8d34477cc2cba0632c88605b22d7294c0840f78596d247c645b1afc0e"));
        // Case-insensitive on input.
        QVERIFY(isPinnedSpkiHex(
            "908769E8D34477CC2CBA0632C88605B22D7294C0840F78596D247C645B1AFC0E"));
        // Not a pin.
        QVERIFY(!isPinnedSpkiHex(std::string(64, 'f')));
        QVERIFY(!isPinnedSpkiHex(""));
    }

    void chainMatchesPinsAcceptAndReject()
    {
        // A fixture "SPKI DER" blob; compute its real digest and inject it as a
        // pin so the ACCEPT path exercises hash + membership end to end.
        const std::string blob = "pretend-spki-der-bytes-\x01\x02\x03";
        const std::string hex = spkiSha256Hex(blob.data(), blob.size());
        QVERIFY(!hex.empty());

        std::vector<std::string> chain = {blob};
        QVERIFY(chainMatchesPins(chain, {hex}));                       // accept
        QVERIFY(chainMatchesPins(chain, {std::string(64, 'a'), hex})); // accept (2nd pin)
        QVERIFY(!chainMatchesPins(chain, {std::string(64, 'a')}));     // reject: no match
        QVERIFY(!chainMatchesPins({}, {hex}));                         // fail closed: no chain
        QVERIFY(!chainMatchesPins(chain, {}));                         // fail closed: no pins
        QVERIFY(!chainMatchesPins({std::string()}, {hex}));            // empty blob ignored -> no match
    }

    void productionChainOfRandomCertsFailsClosed()
    {
        // No random blob will hash into the real pin set.
        std::vector<std::string> chain = {"random-leaf", "random-intermediate", "random-root"};
        QVERIFY(!chainMatchesPinnedSpki(chain));
        QVERIFY(!chainMatchesPinnedSpki({}));
    }

    void pinSetIsIdenticalToNetworkSecurity()
    {
        const std::vector<std::string> brokerPins = pinnedSpkiSet();
        const QStringList qtPins = orion::productionApiPinnedSpkiSha256();

        QCOMPARE(static_cast<int>(brokerPins.size()), qtPins.size());
        QVERIFY2(!brokerPins.empty(), "broker pin set must not be empty");
        for (int i = 0; i < qtPins.size(); ++i) {
            QCOMPARE(QString::fromStdString(brokerPins[static_cast<std::size_t>(i)]),
                     qtPins[i].toLower());
        }
        // Every broker pin is recognized by the broker's own membership test.
        for (const std::string& pin : brokerPins) {
            QVERIFY(isPinnedSpkiHex(pin));
        }
    }
};

QTEST_GUILESS_MAIN(BrokerSpkiPinTests)
#include "BrokerSpkiPinTests.moc"
