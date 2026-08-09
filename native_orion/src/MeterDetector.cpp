#include "MeterDetector.h"

#include <QtCore/QHash>

#if ORION_WITH_OPENCV
#include <opencv2/imgproc.hpp>
#endif

#include <algorithm>
#include <cmath>

namespace orion {

namespace {

double clampPct(double value)
{
    if (!std::isfinite(value)) {
        return 0.0;
    }
    return std::clamp(value, 0.0, 100.0);
}

double rowToPct(double row, int height)
{
    return clampPct(((static_cast<double>(height) - row) / std::max(1.0, static_cast<double>(height))) * 100.0);
}

double colToPct(double col, int width)
{
    return clampPct(((col + 1.0) / std::max(1.0, static_cast<double>(width))) * 100.0);
}

#if ORION_WITH_OPENCV
int nonZeroArea(const cv::Mat& mask, const cv::Rect& rect)
{
    const cv::Rect clipped = rect & cv::Rect(0, 0, mask.cols, mask.rows);
    if (clipped.empty()) {
        return 0;
    }
    return cv::countNonZero(mask(clipped));
}

struct BgrRange {
    cv::Scalar low;
    cv::Scalar high;
};

struct MeterProfile {
    QString name;
    int timingMs = 50;
    int wMin = 1;
    int wMax = 1;
    int hMin = 1;
    int hMax = 1;
    int padL = 0;
    int padT = 0;
    int padR = 0;
    int padB = 0;
    double minConfidence = 0.90;
    double decayRate = 0.01;
    bool vertical = true;
    bool arrowAuxTop = false;
    QHash<QString, BgrRange> colors;
};

const QVector<MeterProfile>& meterProfiles()
{
    static const QVector<MeterProfile> profiles = {
        {
            QStringLiteral("Arrow"), 40,
            37, 45, 18, 24, 16, 15, 16, 15, 0.80, 0.10, false, true,
            {
                {QStringLiteral("Purple"), {{190, 0, 190}, {255, 60, 255}}},
                {QStringLiteral("Yellow"), {{0, 190, 190}, {60, 255, 255}}},
                {QStringLiteral("Red"), {{0, 0, 190}, {60, 60, 255}}},
            }
        },
        {
            QStringLiteral("Arrow2"), 50,
            23, 30, 33, 165, 16, 15, 16, 15, 0.95, 0.01, true, false,
            {
                {QStringLiteral("White"), {{240, 240, 240}, {255, 255, 255}}},
                {QStringLiteral("Purple"), {{220, 0, 220}, {255, 60, 255}}},
                {QStringLiteral("Yellow"), {{0, 190, 190}, {60, 225, 225}}},
                {QStringLiteral("Red"), {{0, 0, 220}, {60, 60, 255}}},
            }
        },
        {
            QStringLiteral("Dial"), 40,
            13, 53, 13, 26, 15, 13, 15, 13, 0.70, 0.10, false, false,
            {
                {QStringLiteral("Purple"), {{190, 0, 190}, {255, 60, 255}}},
                {QStringLiteral("Yellow"), {{0, 190, 190}, {60, 255, 255}}},
                {QStringLiteral("Red"), {{0, 0, 190}, {60, 60, 255}}},
            }
        },
        {
            QStringLiteral("Pill"), 50,
            6, 9, 32, 160, 16, 15, 16, 15, 0.95, 0.01, true, false,
            {
                {QStringLiteral("White"), {{235, 235, 235}, {255, 255, 255}}},
                {QStringLiteral("Purple"), {{190, 0, 190}, {255, 60, 255}}},
                {QStringLiteral("Yellow"), {{0, 190, 190}, {60, 225, 225}}},
                {QStringLiteral("Red"), {{0, 0, 190}, {60, 60, 255}}},
            }
        },
        {
            QStringLiteral("Straight"), 50,
            2, 4, 43, 217, 16, 15, 16, 15, 0.95, 0.01, true, false,
            {
                {QStringLiteral("White"), {{235, 235, 235}, {255, 255, 255}}},
                {QStringLiteral("Purple"), {{190, 0, 190}, {255, 60, 255}}},
                {QStringLiteral("Yellow"), {{0, 190, 190}, {60, 225, 225}}},
                {QStringLiteral("Red"), {{0, 0, 190}, {60, 60, 255}}},
            }
        },
        {
            QStringLiteral("Sword"), 50,
            19, 24, 37, 186, 16, 15, 16, 15, 0.95, 0.01, true, false,
            {
                {QStringLiteral("White"), {{240, 240, 240}, {255, 255, 255}}},
                {QStringLiteral("Purple"), {{220, 0, 220}, {255, 60, 255}}},
                {QStringLiteral("Yellow"), {{0, 190, 190}, {60, 225, 225}}},
                {QStringLiteral("Red"), {{0, 0, 220}, {60, 60, 255}}},
            }
        },
    };
    return profiles;
}

const MeterProfile* profileByName(const QString& name)
{
    for (const auto& profile : meterProfiles()) {
        if (profile.name.compare(name, Qt::CaseInsensitive) == 0) {
            return &profile;
        }
    }
    return nullptr;
}

BgrRange colorRangeForProfile(const MeterProfile& profile, const QString& requestedColor, QString* resolvedColor)
{
    QString key = requestedColor.trimmed();
    if (profile.colors.contains(key)) {
        if (resolvedColor) {
            *resolvedColor = key;
        }
        return profile.colors.value(key);
    }
    if (profile.colors.contains(QStringLiteral("Purple"))) {
        if (resolvedColor) {
            *resolvedColor = QStringLiteral("Purple");
        }
        return profile.colors.value(QStringLiteral("Purple"));
    }
    auto it = profile.colors.constBegin();
    if (resolvedColor) {
        *resolvedColor = it.key();
    }
    return it.value();
}

cv::Rect scaledSearchRoi(const cv::Mat& frameBgr)
{
    const double sx = static_cast<double>(frameBgr.cols) / 1920.0;
    const double sy = static_cast<double>(frameBgr.rows) / 1080.0;
    cv::Rect roi(
        std::clamp(static_cast<int>(std::round(5 * sx)), 0, std::max(0, frameBgr.cols - 1)),
        std::clamp(static_cast<int>(std::round(250 * sy)), 0, std::max(0, frameBgr.rows - 1)),
        std::clamp(static_cast<int>(std::round((1915 - 5) * sx)), 1, frameBgr.cols),
        std::clamp(static_cast<int>(std::round((770 - 250) * sy)), 1, frameBgr.rows));
    roi &= cv::Rect(0, 0, frameBgr.cols, frameBgr.rows);
    return roi.empty() ? cv::Rect(0, 0, frameBgr.cols, frameBgr.rows) : roi;
}

cv::Rect expandedLastRoi(const QRect& lastBbox, const cv::Mat& frameBgr)
{
    if (!lastBbox.isValid()) {
        return {};
    }
    const int growX = std::max(80, lastBbox.width() * 7);
    const int growY = std::max(120, lastBbox.height() * 3);
    cv::Rect roi(
        lastBbox.x() - growX,
        lastBbox.y() - growY,
        lastBbox.width() + growX * 2,
        lastBbox.height() + growY * 2);
    roi &= cv::Rect(0, 0, frameBgr.cols, frameBgr.rows);
    return roi;
}

cv::Mat bgrMask(const cv::Mat& image, const BgrRange& range)
{
    cv::Mat mask;
    cv::inRange(image, range.low, range.high, mask);
    return mask;
}

cv::Mat preprocessMeterRoi(const cv::Mat& roi)
{
    if (roi.empty()) {
        return roi;
    }

    cv::Mat filtered;
    cv::bilateralFilter(roi, filtered, 5, 25.0, 25.0);

    std::vector<cv::Mat> channels;
    cv::split(filtered, channels);
    for (auto& channel : channels) {
        double minValue = 0.0;
        double maxValue = 0.0;
        cv::minMaxLoc(channel, &minValue, &maxValue);
        const double range = maxValue - minValue;
        if (range >= 24.0) {
            channel.convertTo(channel, channel.type(), 255.0 / range, -minValue * 255.0 / range);
        }
    }

    cv::Mat stretched;
    cv::merge(channels, stretched);
    return stretched;
}

cv::Mat robustBgrMask(const cv::Mat& raw, const BgrRange& range)
{
    cv::Mat rawMask = bgrMask(raw, range);
    cv::Mat processedMask;
    const cv::Mat processed = preprocessMeterRoi(raw);
    if (!processed.empty()) {
        processedMask = bgrMask(processed, range);
    }
    if (!processedMask.empty()) {
        cv::bitwise_or(rawMask, processedMask, rawMask);
    }
    return rawMask;
}

bool arrowAuxTopAccepts(const cv::Mat& frameBgr, const cv::Rect& rect)
{
    const int auxHeight = std::clamp(rect.height / 3, 2, 10);
    cv::Rect top(rect.x, std::max(0, rect.y - auxHeight), rect.width, auxHeight);
    top &= cv::Rect(0, 0, frameBgr.cols, frameBgr.rows);
    if (top.empty()) {
        return false;
    }
    cv::Mat aux;
    cv::inRange(frameBgr(top), cv::Scalar(60, 60, 60), cv::Scalar(80, 80, 80), aux);
    const double ratio = static_cast<double>(cv::countNonZero(aux)) /
        std::max(1.0, static_cast<double>(top.width * top.height));
    return ratio >= 0.10;
}

bool profileMatchesContour(const MeterProfile& profile, int w, int h, int frameW, int frameH)
{
    const double sx = static_cast<double>(frameW) / 1920.0;
    const double sy = static_cast<double>(frameH) / 1080.0;
    const double wMin = std::max(1.0, std::floor(profile.wMin * sx));
    const double wMax = std::ceil(profile.wMax * sx) + 2.0;
    const double hMin = std::max(1.0, std::floor(profile.hMin * sy));
    const double hMax = std::ceil(profile.hMax * sy) + 4.0;
    return w >= wMin && w <= wMax && h >= hMin && h <= hMax;
}

cv::Rect profileBoundedBodyFromSeed(const MeterProfile& profile, const cv::Rect& colorRect, int frameW, int frameH)
{
    const double sx = static_cast<double>(frameW) / 1920.0;
    const double sy = static_cast<double>(frameH) / 1080.0;
    const int bodyW = std::max(1, static_cast<int>(std::round(((profile.wMin + profile.wMax) * 0.5) * sx)));
    const int bodyH = std::max(1, static_cast<int>(std::round(((profile.hMin + profile.hMax) * 0.5) * sy)));

    cv::Rect body;
    if (profile.vertical) {
        const int cx = colorRect.x + colorRect.width / 2;
        // Meter fills bottom-to-top. Anchor the fallback body near the colored
        // fill's bottom edge so partial fills still project to the full meter.
        const int bottom = colorRect.y + colorRect.height + std::max(2, static_cast<int>(std::round(profile.padB * sy * 0.25)));
        body = cv::Rect(cx - bodyW / 2, bottom - bodyH, bodyW, bodyH);
    } else {
        const int cy = colorRect.y + colorRect.height / 2;
        const int left = colorRect.x - std::max(2, static_cast<int>(std::round(profile.padL * sx * 0.25)));
        body = cv::Rect(left, cy - bodyH / 2, bodyW, bodyH);
    }
    body &= cv::Rect(0, 0, frameW, frameH);
    return body;
}

cv::Rect boundShadeBodyToProfile(const MeterProfile& profile, const cv::Rect& shadeBody, const cv::Rect& colorRect, int frameW, int frameH)
{
    if (shadeBody.empty()) {
        return {};
    }
    if (profileMatchesContour(profile, shadeBody.width, shadeBody.height, frameW, frameH)) {
        return shadeBody;
    }

    const double sx = static_cast<double>(frameW) / 1920.0;
    const double sy = static_cast<double>(frameH) / 1080.0;
    const int wMin = std::max(1, static_cast<int>(std::floor(profile.wMin * sx)));
    const int wMax = std::max(wMin, static_cast<int>(std::ceil(profile.wMax * sx)) + 2);
    const int hMin = std::max(1, static_cast<int>(std::floor(profile.hMin * sy)));
    const int hMax = std::max(hMin, static_cast<int>(std::ceil(profile.hMax * sy)) + 4);
    const cv::Point seedCenter(colorRect.x + colorRect.width / 2, colorRect.y + colorRect.height / 2);

    // Common side-angle failure: the dark shade frame is attached to a wide
    // floor/reflection blob. Clamp it back around the real colored fill seed
    // instead of rejecting a valid Arrow2/Pill/Sword meter outright.
    if (profile.vertical && shadeBody.height >= hMin && shadeBody.height <= hMax && shadeBody.width > wMax) {
        const int bodyW = std::clamp(std::max(colorRect.width, wMin), wMin, wMax);
        cv::Rect bounded(seedCenter.x - bodyW / 2, shadeBody.y, bodyW, shadeBody.height);
        bounded &= cv::Rect(0, 0, frameW, frameH);
        if (!bounded.empty() && bounded.contains(seedCenter) && profileMatchesContour(profile, bounded.width, bounded.height, frameW, frameH)) {
            return bounded;
        }
    }

    if (profile.vertical && (shadeBody.width > wMax || shadeBody.height > hMax)) {
        const int bodyW = std::clamp(std::max(colorRect.width, wMin), wMin, wMax);
        const int bodyH = std::clamp(
            std::max({colorRect.height, hMin, static_cast<int>(std::round((profile.hMin + profile.hMax) * 0.5 * sy))}),
            hMin,
            hMax);
        const int bottom = std::clamp(colorRect.y + colorRect.height + std::max(1, static_cast<int>(std::round(profile.padB * sy * 0.20))),
                                      bodyH,
                                      frameH);
        cv::Rect bounded(seedCenter.x - bodyW / 2, bottom - bodyH, bodyW, bodyH);
        bounded &= cv::Rect(0, 0, frameW, frameH);
        if (!bounded.empty() && bounded.contains(seedCenter) && profileMatchesContour(profile, bounded.width, bounded.height, frameW, frameH)) {
            return bounded;
        }
    }

    const cv::Rect fallback = profileBoundedBodyFromSeed(profile, colorRect, frameW, frameH);
    if (fallback.empty()) {
        return {};
    }

    // Use the bounded body only when it still contains the actual color seed.
    // This prevents floor/player reflections from replacing a real meter seed
    // while preserving side-angle shots where gray shade expansion grows too far.
    return fallback.contains(seedCenter) ? fallback : cv::Rect{};
}

bool profileMatchesSeedContour(const MeterProfile& profile, int w, int h, int frameW, int frameH)
{
    const double sx = static_cast<double>(frameW) / 1920.0;
    const double sy = static_cast<double>(frameH) / 1080.0;
    const double wMin = std::max(1.0, std::floor(profile.wMin * sx));
    const double wMax = std::ceil(profile.wMax * sx) + 2.0;
    const double hMin = std::max(1.0, std::floor(profile.hMin * sy));
    const double hMax = std::ceil(profile.hMax * sy) + 4.0;
    if (profile.vertical) {
        const double seedHMin = std::max(3.0, std::floor(hMin * 0.18));
        const double seedWMin = std::max(2.0, std::floor(wMin * 0.72));
        return w >= seedWMin && w <= wMax && h >= seedHMin && h <= hMax;
    }
    const double seedWMin = std::max(4.0, std::floor(wMin * 0.18));
    return h >= hMin && h <= hMax && w >= seedWMin && w <= wMax;
}

cv::Rect expandToShadeBody(const cv::Mat& frameBgr, const cv::Rect& colorRect, bool vertical)
{
    const int growX = std::max(18, colorRect.width * 3);
    const int growTop = vertical ? std::max(80, colorRect.height * 2) : std::max(18, colorRect.height * 2);
    const int growBottom = vertical ? std::max(18, colorRect.height / 3) : std::max(18, colorRect.height * 2);
    cv::Rect search(
        colorRect.x - growX,
        colorRect.y - growTop,
        colorRect.width + growX * 2,
        colorRect.height + growTop + growBottom);
    search &= cv::Rect(0, 0, frameBgr.cols, frameBgr.rows);
    if (search.empty()) {
        return colorRect;
    }

    cv::Mat hsv;
    cv::cvtColor(frameBgr(search), hsv, cv::COLOR_BGR2HSV);
    cv::Mat shade;
    cv::inRange(hsv, cv::Scalar(0, 0, 12), cv::Scalar(179, 82, 112), shade);
    cv::morphologyEx(shade, shade, cv::MORPH_CLOSE, cv::getStructuringElement(cv::MORPH_RECT, {3, 3}));

    std::vector<std::vector<cv::Point>> contours;
    cv::findContours(shade, contours, cv::RETR_EXTERNAL, cv::CHAIN_APPROX_SIMPLE);
    const cv::Point colorCenter(colorRect.x + colorRect.width / 2 - search.x,
                                colorRect.y + colorRect.height / 2 - search.y);
    cv::Rect best;
    int bestArea = 0;
    for (const auto& contour : contours) {
        const cv::Rect local = cv::boundingRect(contour);
        if (!local.contains(colorCenter)) {
            continue;
        }
        const int area = local.width * local.height;
        if (area > bestArea) {
            bestArea = area;
            best = local;
        }
    }
    if (best.empty()) {
        return colorRect;
    }
    cv::Rect expanded(search.x + best.x, search.y + best.y, best.width, best.height);
    expanded |= colorRect;
    expanded &= cv::Rect(0, 0, frameBgr.cols, frameBgr.rows);
    return expanded.empty() ? colorRect : expanded;
}

void mergeRange(cv::Mat& dst, const cv::Mat& hsv, const cv::Scalar& low, const cv::Scalar& high)
{
    cv::Mat part;
    cv::inRange(hsv, low, high, part);
    if (dst.empty()) {
        dst = part;
    } else {
        cv::bitwise_or(dst, part, dst);
    }
}

cv::Mat meterFillMask(const cv::Mat& hsv, const QString& configuredColor, bool includeFallbacks)
{
    cv::Mat mask;
    const QString color = configuredColor.toLower();
    const auto addConfigured = [&]() {
        if (color == QLatin1String("white")) {
            mergeRange(mask, hsv, {0, 0, 218}, {179, 68, 255});
        } else if (color == QLatin1String("orange")) {
            mergeRange(mask, hsv, {7, 120, 120}, {27, 255, 255});
        } else if (color == QLatin1String("yellow")) {
            mergeRange(mask, hsv, {22, 105, 110}, {46, 255, 255});
        } else if (color == QLatin1String("red")) {
            mergeRange(mask, hsv, {0, 120, 120}, {13, 255, 255});
            mergeRange(mask, hsv, {168, 120, 120}, {179, 255, 255});
        } else if (color == QLatin1String("green")) {
            mergeRange(mask, hsv, {42, 85, 90}, {88, 255, 255});
        } else if (color == QLatin1String("cyan")) {
            mergeRange(mask, hsv, {84, 90, 105}, {116, 255, 255});
        } else {
            mergeRange(mask, hsv, {132, 80, 90}, {166, 255, 255});
        }
    };

    addConfigured();
    if (includeFallbacks) {
        // Meter color can differ from saved settings after the user changes
        // 2K release style. Search all high-saturation meter colors, then let
        // strict geometry reject rings, court art, names, and UI text.
        mergeRange(mask, hsv, {22, 95, 105}, {46, 255, 255});   // yellow
        mergeRange(mask, hsv, {42, 85, 90}, {88, 255, 255});    // green
        mergeRange(mask, hsv, {7, 110, 115}, {28, 255, 255});   // orange
        mergeRange(mask, hsv, {132, 85, 95}, {166, 255, 255});  // purple
    }
    return mask;
}

double shadeSupportRatio(const cv::Mat& frameBgr, const cv::Rect& rect)
{
    const cv::Rect clipped = rect & cv::Rect(0, 0, frameBgr.cols, frameBgr.rows);
    if (clipped.empty()) {
        return 0.0;
    }
    cv::Mat hsv;
    cv::cvtColor(frameBgr(clipped), hsv, cv::COLOR_BGR2HSV);
    std::vector<cv::Mat> ch;
    cv::split(hsv, ch);
    cv::Mat veryDark;
    cv::Mat mutedShade;
    cv::compare(ch[2], 78, veryDark, cv::CMP_LE);
    cv::Mat lowSat;
    cv::Mat midValue;
    cv::compare(ch[1], 92, lowSat, cv::CMP_LE);
    cv::compare(ch[2], 166, midValue, cv::CMP_LE);
    cv::bitwise_and(lowSat, midValue, mutedShade);
    cv::bitwise_or(veryDark, mutedShade, mutedShade);
    return static_cast<double>(cv::countNonZero(mutedShade)) /
        std::max(1.0, static_cast<double>(clipped.width * clipped.height));
}
#endif

} // namespace

MeterDetector::MeterDetector(QObject* parent)
    : QObject(parent)
{
    clock_.start();
}

void MeterDetector::applyConfig(const AppConfigData& data)
{
    const bool styleChanged = config_.meterStyle.compare(data.meterStyle, Qt::CaseInsensitive) != 0;
    const bool colorChanged = config_.meterColor.compare(data.meterColor, Qt::CaseInsensitive) != 0;
    config_.meterColor = data.meterColor;
    config_.meterStyle = data.meterStyle;
    // Detection confidence is now a bot-owned guardrail. Keep the UI/config
    // value as a hint, but cap it so a meter color/profile mismatch does not
    // completely disable tracking.
    config_.confidenceThreshold = std::clamp(data.detectionConfidencePercent / 100.0, 0.20, 0.95);
    config_.greenWindowStartPct = data.releaseThresholdPct;
    config_.greenWindowEndPct = 100.0;
    config_.totalLatencyMs = data.latencyCompensationMs;
    if (styleChanged || colorChanged) {
        reset();
    }
}

void MeterDetector::reset()
{
    motion_.clear();
    lastResult_ = {};
    lastBbox_ = {};
    lockedStyle_.clear();
    lockRejectFrames_ = 0;
    consecutiveFrames_ = 0;
    memoryFramesLeft_ = 0;
}

#if !ORION_WITH_OPENCV
DetectionResult MeterDetector::detectUnavailable()
{
    DetectionResult result;
    result.style = QStringLiteral("OpenCV unavailable");
    return result;
}
#endif

double MeterDetector::nowMs() const
{
    return static_cast<double>(clock_.nsecsElapsed()) / 1'000'000.0;
}

void MeterDetector::updateMotion(double fillPct, double fillUnits, bool valid, double timestampMs)
{
    if (!valid) {
        return;
    }
    motion_.push_back({fillPct, fillUnits, timestampMs, valid});
    while (motion_.size() > 8) {
        motion_.pop_front();
    }
}

double MeterDetector::velocityPctS() const
{
    if (motion_.size() < 2) {
        return 0.0;
    }
    const auto& first = motion_.front();
    const auto& last = motion_.back();
    const double dtS = (last.timestampMs - first.timestampMs) / 1000.0;
    if (dtS <= 0.0) {
        return 0.0;
    }
    return (last.fillPct - first.fillPct) / dtS;
}

double MeterDetector::accelerationPctS2() const
{
    if (motion_.size() < 4) {
        return 0.0;
    }
    const auto mid = motion_.begin() + static_cast<std::ptrdiff_t>(motion_.size() / 2);
    const auto& first = motion_.front();
    const auto& middle = *mid;
    const auto& last = motion_.back();
    const double dt1 = (middle.timestampMs - first.timestampMs) / 1000.0;
    const double dt2 = (last.timestampMs - middle.timestampMs) / 1000.0;
    if (dt1 <= 0.0 || dt2 <= 0.0) {
        return 0.0;
    }
    const double v1 = (middle.fillPct - first.fillPct) / dt1;
    const double v2 = (last.fillPct - middle.fillPct) / dt2;
    const double dt = (last.timestampMs - first.timestampMs) / 1000.0;
    return dt > 0.0 ? (v2 - v1) / dt : 0.0;
}

double MeterDetector::etaToTargetMs(double fillPct, double targetPct, double velocity, double accel) const
{
    const double remaining = targetPct - fillPct;
    if (remaining <= 0.0) {
        return 0.0;
    }
    if (velocity <= 0.5) {
        return -1.0;
    }

    if (std::abs(accel) > 1e-3) {
        const double a = 0.5 * accel;
        const double b = velocity;
        const double c = -remaining;
        const double disc = b * b - 4.0 * a * c;
        if (disc >= 0.0) {
            const double root = (-b + std::sqrt(disc)) / (2.0 * a);
            if (std::isfinite(root) && root >= 0.0) {
                return root * 1000.0;
            }
        }
    }

    return (remaining / velocity) * 1000.0;
}

bool MeterDetector::stabilityAccept(const Candidate& candidate, double fillPct)
{
    if (!candidate.found || candidate.confidence < config_.confidenceThreshold) {
        consecutiveFrames_ = std::max(0, consecutiveFrames_ - 1);
        return false;
    }

    if (lastBbox_.isValid()) {
        const QPoint ac = candidate.bbox.center();
        const QPoint bc = lastBbox_.center();
        const double dist = std::hypot(static_cast<double>(ac.x() - bc.x()), static_cast<double>(ac.y() - bc.y()));
        const double maxJump = std::max(20.0, std::max(lastBbox_.width(), lastBbox_.height()) * 1.20);
        if (dist > maxJump) {
            consecutiveFrames_ = 0;
            return false;
        }
        const double widthRatio = static_cast<double>(candidate.bbox.width()) / std::max(1.0, static_cast<double>(lastBbox_.width()));
        const double heightRatio = static_cast<double>(candidate.bbox.height()) / std::max(1.0, static_cast<double>(lastBbox_.height()));
        if (widthRatio < 0.55 || widthRatio > 1.80 || heightRatio < 0.55 || heightRatio > 1.80) {
            consecutiveFrames_ = 0;
            return false;
        }
    }

    Q_UNUSED(fillPct);
    consecutiveFrames_++;
    lastBbox_ = candidate.bbox;
    return true;
}

#if ORION_WITH_OPENCV

cv::Scalar MeterDetector::fillHsvLow() const
{
    const auto c = config_.meterColor.toLower();
    if (c == QLatin1String("white")) {
        return {0, 0, 225};
    }
    if (c == QLatin1String("orange")) {
        return {8, 150, 150};
    }
    if (c == QLatin1String("yellow")) {
        return {25, 130, 135};
    }
    if (c == QLatin1String("red")) {
        return {0, 140, 140};
    }
    if (c == QLatin1String("green")) {
        return {50, 120, 120};
    }
    if (c == QLatin1String("cyan")) {
        return {88, 110, 130};
    }
    return {135, 100, 100};
}

cv::Scalar MeterDetector::fillHsvHigh() const
{
    const auto c = config_.meterColor.toLower();
    if (c == QLatin1String("white")) {
        return {179, 55, 255};
    }
    if (c == QLatin1String("orange")) {
        return {24, 255, 255};
    }
    if (c == QLatin1String("yellow")) {
        return {40, 255, 255};
    }
    if (c == QLatin1String("red")) {
        return {12, 255, 255};
    }
    if (c == QLatin1String("green")) {
        return {75, 255, 255};
    }
    if (c == QLatin1String("cyan")) {
        return {112, 255, 255};
    }
    return {162, 255, 255};
}

MeterDetector::Candidate MeterDetector::findMeterCandidate(const cv::Mat& frameBgr) const
{
    // -----------------------------------------------------------------------
    // Meter detection:
    //
    //   1. Focus on the player area (center-ish ROI — not the whole frame).
    //   2. Find a dark-gray FRAME first (the meter background). The gray
    //      frame is the most reliable cue because bright colored UI/jerseys
    //      never produce a tall narrow dark rectangle.
    //   3. Within each gray-frame candidate check that the user's meter
    //      color exists inside — confirming it really is the meter.
    //   4. The bbox covers the FULL gray frame (filled + unfilled) so that
    //      fillPercent can give an accurate 0-100% reading.
    // -----------------------------------------------------------------------
    Candidate best;
    if (frameBgr.empty()) {
        return best;
    }

    const int height = frameBgr.rows;
    const int width  = frameBgr.cols;

    // --- Search region: center-heavy (meter appears near the player) -------
    const int sx = std::clamp(static_cast<int>(width  * 0.15), 0, width  - 1);
    const int sy = std::clamp(static_cast<int>(height * 0.20), 0, height - 1);
    const int sw = std::clamp(static_cast<int>(width  * 0.70), 1, width  - sx);
    const int sh = std::clamp(static_cast<int>(height * 0.72), 1, height - sy);
    const cv::Rect search(sx, sy, sw, sh);
    const cv::Mat roi = frameBgr(search);

    // --- Build two masks in one HSV pass -----------------------------------
    cv::Mat hsv;
    cv::cvtColor(roi, hsv, cv::COLOR_BGR2HSV);

    // Gray mask: the DARK gray meter background / frame.
    // is_gray_pixels / is_gray_shade / verify_contour_sides
    // Range tuned to catch the meter body (dark gray, V ≈ 25-90) but NOT
    // the court floor or jerseys (V > 100 or high saturation).
    cv::Mat grayMask;
    cv::inRange(hsv, cv::Scalar(0, 0, 15), cv::Scalar(179, 55, 100), grayMask);

    // Color mask: prefer the configured color, but include known 2K meter
    // colors as secondary evidence. Users can change meter color in-game; the
    // shaded frame geometry should remain the primary lock.
    cv::Mat strictColorMask = meterFillMask(hsv, config_.meterColor, false);
    cv::Mat colorMask = meterFillMask(hsv, config_.meterColor, true);

    // Close small gaps in both masks
    const cv::Mat kernel3 = cv::getStructuringElement(cv::MORPH_RECT, {3, 3});
    cv::morphologyEx(grayMask,  grayMask,  cv::MORPH_CLOSE, kernel3);
    cv::morphologyEx(strictColorMask, strictColorMask, cv::MORPH_CLOSE, kernel3);
    cv::morphologyEx(colorMask, colorMask, cv::MORPH_CLOSE, kernel3);

    // --- Find gray-frame contours ------------------------------------------
    std::vector<std::vector<cv::Point>> contours;
    cv::findContours(grayMask, contours, cv::RETR_EXTERNAL, cv::CHAIN_APPROX_SIMPLE);

    for (const auto& contour : contours) {
        const double area = cv::contourArea(contour);
        if (area < 120.0) {
            continue;
        }

        const cv::Rect local = cv::boundingRect(contour);

        // --- Shape filter: tall, narrow vertical bar -----------------------
        if (local.width < 4 || local.height < 30) {
            continue;
        }
        const double vAr = static_cast<double>(local.height) / std::max(1.0, static_cast<double>(local.width));
        if (vAr < 2.5 || vAr > 25.0) {
            continue;
        }
        // Meter is thin: max ~30px wide at 1080p, ~0.04 * width
        if (local.width > std::max(36, static_cast<int>(width * 0.04))) {
            continue;
        }
        // Meter max height ~30% of frame
        if (local.height > static_cast<int>(height * 0.35)) {
            continue;
        }

        // --- Gray density: should be well-filled ---------------------------
        const int grayPx = nonZeroArea(grayMask, local);
        const double grayDensity = static_cast<double>(grayPx)
            / std::max(1.0, static_cast<double>(local.width * local.height));
        if (grayDensity < 0.12) {
            continue;
        }

        // --- Must contain some fill color inside (confirms it's the meter) -
        // The fill could be anywhere from 1% to 100% of the frame height.
        // Even a small amount of colored pixels inside the gray frame is a
        // strong signal this is the meter, not a random shadow.
        const int strictColorPx = nonZeroArea(strictColorMask, local);
        const int colorPx = nonZeroArea(colorMask, local);
        const double colorRatio = static_cast<double>(colorPx)
            / std::max(1.0, static_cast<double>(local.width * local.height));
        if (colorPx < 2 || colorRatio < 0.008) {
            continue;
        }

        // --- Side verification (verify_contour_sides) ----------------
        // Check that pixels immediately left AND right of the candidate are
        // NOT gray (i.e. the gray region ends, proving it's a discrete bar
        // rather than a large shadow or floor area).
        bool sidesValid = true;
        const int sideCheckW = std::max(3, local.width / 2 + 2);
        for (int side = 0; side < 2; ++side) {
            cv::Rect sideRect;
            if (side == 0) { // left
                sideRect = cv::Rect(
                    std::max(0, local.x - sideCheckW), local.y,
                    std::min(sideCheckW, local.x), local.height);
            } else { // right
                const int rx = local.x + local.width;
                sideRect = cv::Rect(
                    rx, local.y,
                    std::min(sideCheckW, hsv.cols - rx), local.height);
            }
            if (sideRect.width <= 0 || sideRect.height <= 0) {
                continue;
            }
            const int sideGray = nonZeroArea(grayMask, sideRect);
            const double sideGrayRatio = static_cast<double>(sideGray)
                / std::max(1.0, static_cast<double>(sideRect.width * sideRect.height));
            // If the side is ALSO gray, this is a wall/shadow, not a meter
            if (sideGrayRatio > 0.55) {
                sidesValid = false;
                break;
            }
        }
        if (!sidesValid) {
            continue;
        }

        // --- Build full-frame bbox -----------------------------------------
        const int bboxW = std::clamp(local.width + 4, 8, std::max(22, static_cast<int>(width * 0.04)));
        const int centerX = search.x + local.x + local.width / 2;
        const int topY    = search.y + local.y;
        const int botY    = search.y + local.y + local.height;
        const int bx = std::clamp(centerX - bboxW / 2, 0, width - bboxW);
        const int by = std::clamp(topY - 2, 0, height - local.height);
        const int bh = std::clamp(botY - by + 2, 30, height - by);

        const QRect bbox(bx, by, bboxW, bh);

        // --- Confidence scoring -------------------------------------------
        const double arScore = 1.0 - std::min(1.0, std::abs(vAr - 7.0) / 10.0);
        const double posY = static_cast<double>(bbox.center().y()) / std::max(1.0, static_cast<double>(height));
        const double posScore = (posY < 0.25 || posY > 0.95) ? 0.4 : 1.0;
        const double strictBoost = strictColorPx > 0 ? 0.10 : 0.0;
        const cv::Rect shadeRect(
            std::max(0, bbox.x() - 4),
            std::max(0, bbox.y() - 4),
            std::min(frameBgr.cols - std::max(0, bbox.x() - 4), bbox.width() + 8),
            std::min(frameBgr.rows - std::max(0, bbox.y() - 4), bbox.height() + 8)
        );
        const double shadeSupport = shadeSupportRatio(frameBgr, shadeRect);
        const double confidence = std::clamp(
            (grayDensity * 0.28 + colorRatio * 0.22 + shadeSupport * 0.20 + arScore * 0.20 + 0.10 + strictBoost) * posScore,
            0.0, 1.0);

        if (confidence > best.confidence) {
            best.found = true;
            best.bbox = bbox;
            best.style = config_.meterStyle;
            best.confidence = confidence;
            best.vertical = true;
        }
    }

    return best;
}

double MeterDetector::fillPercent(const cv::Mat& frameBgr, const Candidate& candidate, double* fillUnits) const
{
    // -----------------------------------------------------------------------
    // Cosmic Vision "topmost_color" approach:
    //
    //   The bbox now covers the FULL meter (gray frame = filled + unfilled).
    //   Scan rows from bottom to top: the topmost row with colored pixels
    //   is the current fill level.  fill% = colored_rows / total_height.
    //
    //   This eliminates the old circular bug where bbox height was derived
    //   from fill height, making fill% always ~54%.
    // -----------------------------------------------------------------------
    if (!candidate.found || frameBgr.empty()) {
        if (fillUnits) {
            *fillUnits = 0.0;
        }
        return 0.0;
    }

    const auto r = candidate.bbox;
    const cv::Rect rect(
        std::clamp(r.x(), 0, frameBgr.cols - 1),
        std::clamp(r.y(), 0, frameBgr.rows - 1),
        std::clamp(r.width(), 1, frameBgr.cols - std::clamp(r.x(), 0, frameBgr.cols - 1)),
        std::clamp(r.height(), 1, frameBgr.rows - std::clamp(r.y(), 0, frameBgr.rows - 1))
    );

    cv::Mat hsv;
    cv::cvtColor(frameBgr(rect), hsv, cv::COLOR_BGR2HSV);
    cv::Mat mask = meterFillMask(hsv, config_.meterColor, false);
    if (cv::countNonZero(mask) < 2) {
        mask = meterFillMask(hsv, config_.meterColor, true);
    }

    if (candidate.vertical) {
        // Trim narrow margins on sides to avoid border artifacts
        const int xMargin = std::min(rect.width / 5, std::max(0, rect.width / 2 - 3));
        cv::Mat strip = mask;
        if (xMargin > 0 && rect.width - xMargin * 2 >= 3) {
            strip = mask(cv::Rect(xMargin, 0, rect.width - xMargin * 2, rect.height));
        }

        // Row projection: count colored pixels per row
        cv::Mat projection;
        cv::reduce(strip > 0, projection, 1, cv::REDUCE_SUM, CV_32S);
        const int rowThreshold = std::max(1, strip.cols / 5);

        // Find topmost colored row (Cosmic "topmost_color")
        // and bottommost colored row ("bottommost_color_y")
        int topmostRow = -1;
        int bottomRow  = -1;
        for (int rIdx = 0; rIdx < projection.rows; ++rIdx) {
            if (projection.at<int>(rIdx, 0) >= rowThreshold) {
                if (topmostRow < 0) {
                    topmostRow = rIdx;
                }
                bottomRow = rIdx;
            }
        }

        if (topmostRow < 0) {
            if (fillUnits) { *fillUnits = 0.0; }
            return 0.0;
        }

        // Fill height = from bottom of meter to topmost colored row.
        // Meter fills from bottom to top, so fill = (meterBottom - topmostRow).
        // The bbox covers the full meter: bottom is rect.height-1, top is 0.
        const double filledHeight = static_cast<double>(rect.height - topmostRow);
        if (fillUnits) {
            *fillUnits = filledHeight;
        }
        return clampPct(filledHeight / std::max(1.0, static_cast<double>(rect.height)) * 100.0);
    }

    // Horizontal meter: same logic but columns left-to-right
    cv::Mat projection;
    cv::reduce(mask > 0, projection, 0, cv::REDUCE_SUM, CV_32S);
    const int colThreshold = std::max(1, rect.height / 5);
    int rightmostCol = -1;
    for (int cIdx = 0; cIdx < projection.cols; ++cIdx) {
        if (projection.at<int>(0, cIdx) >= colThreshold) {
            rightmostCol = cIdx;
        }
    }
    if (rightmostCol < 0) {
        if (fillUnits) { *fillUnits = 0.0; }
        return 0.0;
    }
    const double filledWidth = static_cast<double>(rightmostCol + 1);
    if (fillUnits) {
        *fillUnits = filledWidth;
    }
    return clampPct(filledWidth / std::max(1.0, static_cast<double>(rect.width)) * 100.0);
}

MeterDetector::GreenWindow MeterDetector::scanGreenWindow(const cv::Mat& frameBgr, const Candidate& candidate) const
{
    GreenWindow out;
    if (!candidate.found || frameBgr.empty()) {
        return out;
    }

    const auto qr = candidate.bbox;
    cv::Rect rect(
        std::clamp(qr.x(), 0, frameBgr.cols - 1),
        std::clamp(qr.y(), 0, frameBgr.rows - 1),
        std::clamp(qr.width(), 1, frameBgr.cols - std::clamp(qr.x(), 0, frameBgr.cols - 1)),
        std::clamp(qr.height(), 1, frameBgr.rows - std::clamp(qr.y(), 0, frameBgr.rows - 1))
    );
    if (rect.empty()) {
        return out;
    }

    cv::Mat roi = frameBgr(rect);
    const bool upscaled = (candidate.vertical ? rect.height : rect.width) < 40;
    if (upscaled) {
        cv::resize(roi, roi, {rect.width * 2, rect.height * 2}, 0.0, 0.0, cv::INTER_LINEAR);
    }

    cv::Mat hsv;
    cv::cvtColor(roi, hsv, cv::COLOR_BGR2HSV);

    cv::Mat neon;
    cv::Mat broad;
    cv::inRange(hsv, cv::Scalar(48, 140, 140), cv::Scalar(70, 255, 255), neon);
    cv::inRange(hsv, cv::Scalar(35, 80, 80), cv::Scalar(90, 255, 255), broad);

    std::vector<cv::Mat> channels;
    cv::split(roi, channels);
    cv::Mat dominance;
    cv::compare(channels[1], 118, dominance, cv::CMP_GE);
    cv::Mat greenOverRed;
    cv::Mat greenOverBlue;
    cv::subtract(channels[1], channels[2], greenOverRed, cv::noArray(), CV_16S);
    cv::subtract(channels[1], channels[0], greenOverBlue, cv::noArray(), CV_16S);
    cv::Mat domR;
    cv::Mat domB;
    cv::compare(greenOverRed, 34, domR, cv::CMP_GE);
    cv::compare(greenOverBlue, 24, domB, cv::CMP_GE);
    cv::bitwise_and(dominance, domR, dominance);
    cv::bitwise_and(dominance, domB, dominance);

    cv::Mat mask = cv::countNonZero(neon) >= 2 ? neon : broad;
    if (cv::countNonZero(dominance) >= 2) {
        cv::bitwise_or(mask, dominance, mask);
    }
    cv::morphologyEx(
        mask,
        mask,
        cv::MORPH_CLOSE,
        cv::getStructuringElement(cv::MORPH_ELLIPSE, {upscaled ? 2 : 3, upscaled ? 2 : 3}),
        {-1, -1},
        1
    );

    out.clusterPx = cv::countNonZero(mask);
    if (out.clusterPx < config_.greenClusterMinPx) {
        return out;
    }

    if (candidate.vertical) {
        cv::Mat projection;
        cv::reduce(mask > 0, projection, 1, cv::REDUCE_SUM, CV_32S);
        int top = -1;
        int bottom = -1;
        const int rowThreshold = std::max(1, mask.cols / 12);
        for (int i = 0; i < projection.rows; ++i) {
            if (projection.at<int>(i, 0) >= rowThreshold) {
                if (top < 0) {
                    top = i;
                }
                bottom = i;
            }
        }
        if (top < 0 || bottom < 0) {
            return out;
        }
        const int effectiveHeight = rect.height * (upscaled ? 2 : 1);
        out.startPct = rowToPct(bottom, effectiveHeight);
        out.endPct = rowToPct(top, effectiveHeight);
        out.centerPct = rowToPct((top + bottom) * 0.5, effectiveHeight);
        out.widthPct = std::max(100.0 / std::max(1, effectiveHeight), std::abs(out.endPct - out.startPct));
        const int span = std::max(1, bottom - top + 1);
        const double spanRatio = static_cast<double>(span) / std::max(1, mask.rows);
        out.confidence = std::clamp((static_cast<double>(out.clusterPx) / std::max(1.0, span * static_cast<double>(mask.cols))) * 0.65 + (1.0 - spanRatio) * 0.35, 0.05, 1.0);
        out.found = true;
        return out;
    }

    cv::Mat projection;
    cv::reduce(mask > 0, projection, 0, cv::REDUCE_SUM, CV_32S);
    int left = -1;
    int right = -1;
    const int colThreshold = std::max(1, mask.rows / 12);
    for (int i = 0; i < projection.cols; ++i) {
        if (projection.at<int>(0, i) >= colThreshold) {
            if (left < 0) {
                left = i;
            }
            right = i;
        }
    }
    if (left < 0 || right < 0) {
        return out;
    }

    const int effectiveWidth = rect.width * (upscaled ? 2 : 1);
    out.startPct = colToPct(left, effectiveWidth);
    out.endPct = colToPct(right, effectiveWidth);
    out.centerPct = colToPct((left + right) * 0.5, effectiveWidth);
    out.widthPct = std::max(100.0 / std::max(1, effectiveWidth), std::abs(out.endPct - out.startPct));
    const int span = std::max(1, right - left + 1);
    const double spanRatio = static_cast<double>(span) / std::max(1, mask.cols);
    out.confidence = std::clamp((static_cast<double>(out.clusterPx) / std::max(1.0, span * static_cast<double>(mask.rows))) * 0.65 + (1.0 - spanRatio) * 0.35, 0.05, 1.0);
    out.found = true;
    return out;
}

MeterDetector::Candidate MeterDetector::findMeterCandidateBgr(const cv::Mat& frameBgr) const
{
    Candidate best;
    if (frameBgr.empty()) {
        best.rejectionReason = QStringLiteral("empty frame");
        return best;
    }

    const auto* preferred = profileByName(config_.meterStyle);
    QVector<const MeterProfile*> profiles;
    if (preferred) {
        profiles.push_back(preferred);
    }
    if (!preferred) {
        for (const auto& profile : meterProfiles()) {
            profiles.push_back(&profile);
        }
    }

    const cv::Rect fullRoi = scaledSearchRoi(frameBgr);
    const cv::Rect lockedRoi = expandedLastRoi(lastBbox_, frameBgr);
    const cv::Rect search = !lockedRoi.empty() ? lockedRoi : fullRoi;
    best.searchRoi = QRect(search.x, search.y, search.width, search.height);
    if (search.empty()) {
        best.rejectionReason = QStringLiteral("empty search roi");
        return best;
    }

    const cv::Mat rawRoi = frameBgr(search);
    const cv::Mat kernel = cv::getStructuringElement(cv::MORPH_RECT, {2, 2});
    int candidateCount = 0;
    QString lastReject = QStringLiteral("no BGR contour match");
    QRect bestRejected;
    double bestRejectedScore = 0.0;

    for (const auto* profile : profiles) {
        QString resolvedColor;
        const BgrRange range = colorRangeForProfile(*profile, config_.meterColor, &resolvedColor);
        cv::Mat mask = robustBgrMask(rawRoi, range);
        cv::morphologyEx(mask, mask, cv::MORPH_CLOSE, kernel);
        cv::morphologyEx(mask, mask, cv::MORPH_OPEN, kernel);

        std::vector<std::vector<cv::Point>> contours;
        cv::findContours(mask, contours, cv::RETR_EXTERNAL, cv::CHAIN_APPROX_SIMPLE);
        for (const auto& contour : contours) {
            const cv::Rect local = cv::boundingRect(contour);
            if (!profileMatchesSeedContour(*profile, local.width, local.height, frameBgr.cols, frameBgr.rows)) {
                lastReject = QStringLiteral("%1 contour size rejected").arg(profile->name);
                continue;
            }

            ++candidateCount;
            const cv::Rect colorFrameRect(search.x + local.x, search.y + local.y, local.width, local.height);
            const cv::Rect rawShadeBody = expandToShadeBody(frameBgr, colorFrameRect, profile->vertical);
            cv::Rect shadeBody = boundShadeBodyToProfile(*profile, rawShadeBody, colorFrameRect, frameBgr.cols, frameBgr.rows);
            if (shadeBody.empty() || !profileMatchesContour(*profile, shadeBody.width, shadeBody.height, frameBgr.cols, frameBgr.rows)) {
                const cv::Rect fallback = profileBoundedBodyFromSeed(*profile, colorFrameRect, frameBgr.cols, frameBgr.rows);
                const bool arrow2SideFallback =
                    profile->name.compare(QStringLiteral("Arrow2"), Qt::CaseInsensitive) == 0
                    && !fallback.empty()
                    && profileMatchesContour(*profile, fallback.width, fallback.height, frameBgr.cols, frameBgr.rows)
                    && shadeSupportRatio(frameBgr, fallback) >= 0.010;
                if (arrow2SideFallback) {
                    shadeBody = fallback;
                } else {
                    lastReject = QStringLiteral("%1 shaded body size rejected").arg(profile->name);
                }
                if (!arrow2SideFallback && !fallback.empty()) {
                    const double rejectScore = static_cast<double>(local.width * local.height);
                    if (rejectScore > bestRejectedScore) {
                        bestRejectedScore = rejectScore;
                        bestRejected = QRect(fallback.x, fallback.y, fallback.width, fallback.height);
                    }
                }
                if (!arrow2SideFallback) {
                    continue;
                }
            }
            if (profile->arrowAuxTop && !arrowAuxTopAccepts(frameBgr, shadeBody)) {
                lastReject = QStringLiteral("Arrow aux top rejected");
                bestRejected = QRect(shadeBody.x, shadeBody.y, shadeBody.width, shadeBody.height);
                bestRejectedScore = std::max(bestRejectedScore, static_cast<double>(shadeBody.width * shadeBody.height));
                continue;
            }

            const int colorPx = cv::countNonZero(mask(local));
            const double colorDensity = static_cast<double>(colorPx) /
                std::max(1.0, static_cast<double>(local.width * local.height));
            const double shadeSupport = shadeSupportRatio(frameBgr, shadeBody);
            const double shadeSupportFloor = profile->name.compare(QStringLiteral("Arrow2"), Qt::CaseInsensitive) == 0
                ? 0.012
                : 0.025;
            if (shadeSupport < shadeSupportFloor) {
                lastReject = QStringLiteral("%1 shade support rejected").arg(profile->name);
                const double rejectScore = colorDensity * static_cast<double>(local.width * local.height);
                if (rejectScore > bestRejectedScore) {
                    bestRejectedScore = rejectScore;
                    bestRejected = QRect(shadeBody.x, shadeBody.y, shadeBody.width, shadeBody.height);
                }
                continue;
            }
            const double sizeCenterW = (profile->wMin + profile->wMax) * 0.5 * frameBgr.cols / 1920.0;
            const double sizeCenterH = (profile->hMin + profile->hMax) * 0.5 * frameBgr.rows / 1080.0;
            const double wScore = 1.0 - std::min(1.0, std::abs(shadeBody.width - sizeCenterW) / std::max(2.0, sizeCenterW));
            const double hScore = 1.0 - std::min(1.0, std::abs(shadeBody.height - sizeCenterH) / std::max(2.0, sizeCenterH));
            const double configuredBoost = profile->name.compare(config_.meterStyle, Qt::CaseInsensitive) == 0 ? 0.08 : 0.0;
            const double confidence = std::clamp(
                colorDensity * 0.42 + shadeSupport * 0.20 + wScore * 0.14 + hScore * 0.14 + 0.10 + configuredBoost,
                0.0,
                1.0);
            const double floor = std::min(profile->minConfidence, std::max(0.20, config_.confidenceThreshold));
            if (confidence < floor) {
                lastReject = QStringLiteral("%1 confidence %2 below %3")
                    .arg(profile->name)
                    .arg(confidence, 0, 'f', 2)
                    .arg(floor, 0, 'f', 2);
                const double rejectScore = confidence * 10000.0 + static_cast<double>(local.width * local.height);
                if (rejectScore > bestRejectedScore) {
                    bestRejectedScore = rejectScore;
                    bestRejected = QRect(shadeBody.x, shadeBody.y, shadeBody.width, shadeBody.height);
                }
                continue;
            }

            if (confidence > best.confidence) {
                best.found = true;
                best.bbox = QRect(shadeBody.x, shadeBody.y, shadeBody.width, shadeBody.height);
                best.searchRoi = QRect(search.x, search.y, search.width, search.height);
                best.style = profile->name;
                best.colorName = resolvedColor;
                best.confidence = confidence;
                best.timingMs = profile->timingMs;
                best.minConfidence = profile->minConfidence;
                best.vertical = profile->vertical;
                best.candidateCount = candidateCount;
                best.rejectionReason.clear();
            }
        }
    }

    if (!best.found) {
        best.candidateCount = candidateCount;
        best.rejectionReason = lastReject;
        best.rejectedBbox = bestRejected;
    }
    return best;
}

double MeterDetector::fillPercentBgr(const cv::Mat& frameBgr, const Candidate& candidate, double* fillUnits) const
{
    if (!candidate.found || frameBgr.empty()) {
        if (fillUnits) {
            *fillUnits = 0.0;
        }
        return 0.0;
    }

    const auto r = candidate.bbox;
    cv::Rect rect(
        std::clamp(r.x(), 0, frameBgr.cols - 1),
        std::clamp(r.y(), 0, frameBgr.rows - 1),
        std::clamp(r.width(), 1, frameBgr.cols - std::clamp(r.x(), 0, frameBgr.cols - 1)),
        std::clamp(r.height(), 1, frameBgr.rows - std::clamp(r.y(), 0, frameBgr.rows - 1)));
    if (rect.empty()) {
        if (fillUnits) {
            *fillUnits = 0.0;
        }
        return 0.0;
    }

    const auto* profile = profileByName(candidate.style);
    if (!profile) {
        return fillPercent(frameBgr, candidate, fillUnits);
    }

    QString resolvedColor;
    const BgrRange range = colorRangeForProfile(*profile, candidate.colorName.isEmpty() ? config_.meterColor : candidate.colorName, &resolvedColor);
    cv::Mat mask = robustBgrMask(frameBgr(rect), range);
    if (cv::countNonZero(mask) < 2) {
        return 0.0;
    }

    if (candidate.vertical) {
        const int xMargin = std::min(rect.width / 5, std::max(0, rect.width / 2 - 3));
        cv::Mat strip = mask;
        if (xMargin > 0 && rect.width - xMargin * 2 >= 3) {
            strip = mask(cv::Rect(xMargin, 0, rect.width - xMargin * 2, rect.height));
        }
        cv::Mat projection;
        cv::reduce(strip > 0, projection, 1, cv::REDUCE_SUM, CV_32S);
        const int rowThreshold = std::max(1, strip.cols / 5);
        int topmostRow = -1;
        for (int row = 0; row < projection.rows; ++row) {
            if (projection.at<int>(row, 0) >= rowThreshold) {
                topmostRow = row;
                break;
            }
        }
        if (topmostRow < 0) {
            if (fillUnits) {
                *fillUnits = 0.0;
            }
            return 0.0;
        }
        const double filledHeight = static_cast<double>(rect.height - topmostRow);
        if (fillUnits) {
            *fillUnits = filledHeight;
        }
        return clampPct(filledHeight / std::max(1.0, static_cast<double>(rect.height)) * 100.0);
    }

    cv::Mat projection;
    cv::reduce(mask > 0, projection, 0, cv::REDUCE_SUM, CV_32S);
    const int colThreshold = std::max(1, rect.height / 5);
    int rightmostCol = -1;
    for (int col = 0; col < projection.cols; ++col) {
        if (projection.at<int>(0, col) >= colThreshold) {
            rightmostCol = col;
        }
    }
    if (rightmostCol < 0) {
        if (fillUnits) {
            *fillUnits = 0.0;
        }
        return 0.0;
    }
    const double filledWidth = static_cast<double>(rightmostCol + 1);
    if (fillUnits) {
        *fillUnits = filledWidth;
    }
    return clampPct(filledWidth / std::max(1.0, static_cast<double>(rect.width)) * 100.0);
}

DetectionResult MeterDetector::detect(const cv::Mat& frameBgr)
{
    DetectionResult empty;
    empty.colorName = config_.meterColor;
    if (frameBgr.empty()) {
        emit detectionReady(empty);
        return empty;
    }

    const double timestampMs = nowMs();
    const Candidate candidate = findMeterCandidateBgr(frameBgr);

    double fillUnits = 0.0;
    const double fillPct = candidate.found ? fillPercentBgr(frameBgr, candidate, &fillUnits) : 0.0;
    const bool valid = stabilityAccept(candidate, fillPct);
    if (candidate.found && valid) {
        lockedStyle_ = candidate.style;
        lockRejectFrames_ = 0;
    } else if (!candidate.found || !valid) {
        ++lockRejectFrames_;
        if (lockRejectFrames_ >= 5) {
            lastBbox_ = {};
            lockedStyle_.clear();
        }
    }
    updateMotion(fillPct, fillUnits, valid, timestampMs);

    const double velocity = velocityPctS();
    const double accel = accelerationPctS2();
    const double profileTiming = candidate.timingMs > 0.0 ? candidate.timingMs : config_.totalLatencyMs;
    const double latencyS = profileTiming / 1000.0;
    const double prediction = clampPct(fillPct + velocity * latencyS + 0.5 * accel * latencyS * latencyS);

    GreenWindow green;
    if (candidate.found) {
        green = scanGreenWindow(frameBgr, candidate);
    }

    double gwStart = green.found ? green.startPct : config_.greenWindowStartPct;
    double gwEnd = green.found ? green.endPct : config_.greenWindowEndPct;
    if (gwEnd < gwStart) {
        std::swap(gwStart, gwEnd);
    }
    const double gwCenter = green.found ? green.centerPct : ((gwStart + gwEnd) * 0.5);
    const double eta = etaToTargetMs(fillPct, gwCenter, velocity, accel);

    const bool releaseReady =
        candidate.found
        && valid
        && candidate.confidence >= config_.confidenceThreshold
        && (prediction >= gwCenter || (eta >= 0.0 && eta <= profileTiming) || green.clusterPx >= config_.greenClusterMinPx);

    DetectionResult result;
    result.detected = candidate.found && valid;
    result.style = candidate.style;
    result.profileName = candidate.style;
    result.colorName = candidate.colorName.isEmpty() ? config_.meterColor : candidate.colorName;
    result.rejectionReason = candidate.rejectionReason;
    result.x = candidate.bbox.x();
    result.y = candidate.bbox.y();
    result.width = candidate.bbox.width();
    result.height = candidate.bbox.height();
    result.searchX = candidate.searchRoi.x();
    result.searchY = candidate.searchRoi.y();
    result.searchWidth = candidate.searchRoi.width();
    result.searchHeight = candidate.searchRoi.height();
    result.rejectedX = candidate.rejectedBbox.x();
    result.rejectedY = candidate.rejectedBbox.y();
    result.rejectedWidth = candidate.rejectedBbox.width();
    result.rejectedHeight = candidate.rejectedBbox.height();
    result.fillPct = fillPct;
    result.confidence = candidate.confidence;
    result.consecutiveFrames = consecutiveFrames_;
    result.velocityPctS = velocity;
    result.accelerationPctS2 = accel;
    result.predictionPct = prediction;
    result.targetPct = gwCenter;
    result.profileTimingMs = profileTiming;
    result.candidateCount = candidate.candidateCount;
    result.releaseReady = releaseReady;
    result.velocityStable = std::abs(velocity) > 0.5;
    result.greenClusterPx = green.clusterPx;
    result.greenStartPct = gwStart;
    result.greenEndPct = gwEnd;
    result.greenCenterPct = gwCenter;
    result.greenWidthPct = green.found ? green.widthPct : std::abs(gwEnd - gwStart);
    result.greenConfidence = green.found ? green.confidence : 0.0;
    result.etaToGreenMs = eta;
    result.roiLocked = lastBbox_.isValid();

    if (result.detected) {
        lastResult_ = result;
        memoryFramesLeft_ = 3;
    } else if (memoryFramesLeft_ > 0 && lastResult_.detected) {
        memoryFramesLeft_--;
        result = lastResult_;
        result.confidence *= 0.82;
        result.releaseReady = false;
    }

    emit detectionReady(result);
    return result;
}

#endif

} // namespace orion
