#pragma once

#include "AppConfig.h"
#include "OrionExports.h"
#include "OrionTypes.h"

#include <QtCore/QElapsedTimer>
#include <QtCore/QObject>
#include <QtCore/QRect>
#include <QtCore/QVector>

#if ORION_WITH_OPENCV
#include <opencv2/core.hpp>
#endif

#include <deque>

namespace orion {

struct MeterDetectorConfig {
    QString meterColor = QStringLiteral("Purple");
    QString meterStyle = QStringLiteral("Arrow");
    double confidenceThreshold = 0.35;
    double greenWindowStartPct = 93.0;
    double greenWindowEndPct = 100.0;
    int minConsecutiveFrames = 3;
    int greenClusterMinPx = 3;
    double totalLatencyMs = 45.0;
};

class ORION_VISION_API MeterDetector final : public QObject {
    Q_OBJECT
public:
    explicit MeterDetector(QObject* parent = nullptr);

    void applyConfig(const AppConfigData& data);
    void reset();

#if ORION_WITH_OPENCV
    [[nodiscard]] DetectionResult detect(const cv::Mat& frameBgr);
#else
    [[nodiscard]] DetectionResult detectUnavailable();
#endif

    [[nodiscard]] MeterDetectorConfig config() const { return config_; }

signals:
    void detectionReady(orion::DetectionResult result);

private:
    struct MotionSample {
        double fillPct = 0.0;
        double fillUnits = 0.0;
        double timestampMs = 0.0;
        bool valid = false;
    };

    struct GreenWindow {
        bool found = false;
        int clusterPx = 0;
        double startPct = -1.0;
        double endPct = -1.0;
        double centerPct = -1.0;
        double widthPct = 0.0;
        double confidence = 0.0;
    };

    struct Candidate {
        bool found = false;
        QRect bbox;
        QRect rejectedBbox;
        QRect searchRoi;
        QString style;
        QString colorName;
        QString rejectionReason;
        double confidence = 0.0;
        double timingMs = 0.0;
        double minConfidence = 0.0;
        int candidateCount = 0;
        bool vertical = true;
    };

    [[nodiscard]] double nowMs() const;
    void updateMotion(double fillPct, double fillUnits, bool valid, double timestampMs);
    [[nodiscard]] double velocityPctS() const;
    [[nodiscard]] double accelerationPctS2() const;
    [[nodiscard]] double etaToTargetMs(double fillPct, double targetPct, double velocityPctS, double accelPctS2) const;
    [[nodiscard]] bool stabilityAccept(const Candidate& candidate, double fillPct);

#if ORION_WITH_OPENCV
    [[nodiscard]] Candidate findMeterCandidate(const cv::Mat& frameBgr) const;
    [[nodiscard]] Candidate findMeterCandidateBgr(const cv::Mat& frameBgr) const;
    [[nodiscard]] double fillPercent(const cv::Mat& frameBgr, const Candidate& candidate, double* fillUnits) const;
    [[nodiscard]] double fillPercentBgr(const cv::Mat& frameBgr, const Candidate& candidate, double* fillUnits) const;
    [[nodiscard]] GreenWindow scanGreenWindow(const cv::Mat& frameBgr, const Candidate& candidate) const;
    [[nodiscard]] cv::Scalar fillHsvLow() const;
    [[nodiscard]] cv::Scalar fillHsvHigh() const;
#endif

    MeterDetectorConfig config_;
    QElapsedTimer clock_;
    std::deque<MotionSample> motion_;
    DetectionResult lastResult_;
    QRect lastBbox_;
    QString lockedStyle_;
    int lockRejectFrames_ = 0;
    int consecutiveFrames_ = 0;
    int memoryFramesLeft_ = 0;
};

} // namespace orion
