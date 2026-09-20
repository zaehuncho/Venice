#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <limits>

// Pure mathematics, not release authority or an empirical accuracy estimate.
// A finite sigma is a caller-supplied model parameter, not proof of calibration.
// No geometry, latency, variance, or independence defaults are invented here.
namespace orion::GreenWindowMath {

inline constexpr double unknown = std::numeric_limits<double>::quiet_NaN();

struct Interval {
    double startMs = unknown;
    double endMs = unknown;

    [[nodiscard]] bool valid() const noexcept
    {
        return std::isfinite(startMs) && std::isfinite(endMs) && startMs <= endMs;
    }
};

struct GaussianError {
    double biasMs = unknown;
    double sigmaMs = unknown;

    [[nodiscard]] bool valid() const noexcept
    {
        return std::isfinite(biasMs) && std::isfinite(sigmaMs) && sigmaMs >= 0.0;
    }
};

namespace detail {
// Preserve a finite standardized distance when the raw difference overflows.
[[nodiscard]] inline double standardizedDifference(double a, double b, double sigma) noexcept
{
    const double difference = a - b;
    return std::isfinite(difference) ? difference / sigma : a / sigma - b / sigma;
}

[[nodiscard]] inline double finiteMidpoint(double a, double b) noexcept
{
    // Same-sign subtraction is safe and retains subnormal resolution. The half
    // sums avoid overflow for opposite signs near the limits of double.
    return std::signbit(a) == std::signbit(b) ? a + (b - a) * 0.5 : a * 0.5 + b * 0.5;
}

// Retain low-order terms through cancellation without relying on long double:
// the supported Windows compiler gives long double only double precision. An
// error-free TwoSum expansion also retains cancellation inside the correction,
// where a single compensated accumulator can still erase a small residual.
template<std::size_t Capacity>
struct CompensatedSum {
    std::array<double, Capacity> expansion{};
    std::size_t size = 0;

    void add(double value) noexcept
    {
        if (value == 0.0) return;
        std::size_t nextSize = 0;
        for (std::size_t i = 0; i < size; ++i) {
            const double term = expansion[i];
            const double next = value + term;
            const double termPart = next - value;
            const double error = (value - (next - termPart)) + (term - termPart);
            if (error != 0.0) expansion[nextSize++] = error;
            value = next;
        }
        if (value != 0.0) expansion[nextSize++] = value;
        size = nextSize;
    }

    [[nodiscard]] double value() const noexcept
    {
        double result = 0.0;
        for (std::size_t i = 0; i < size; ++i) result += expansion[i];
        return result;
    }
};
} // namespace detail

// Conditional on a FIXED, correctly identified closed interval, and on the
// supplied Gaussian error law. Uncertain green boundaries are not marginalized
// by this function. Do not call its output a calibrated success probability.
// Sigma=0 is an atom: endpoints count as inside; a zero-width interval has
// probability one only if that deterministic atom equals its endpoint.
[[nodiscard]] inline double gaussianIntervalProbability(
    const Interval& window, double commandMs, const GaussianError& error) noexcept
{
    if (!window.valid() || !error.valid() || !std::isfinite(commandMs)) return unknown;
    const double mean = commandMs + error.biasMs;
    if (!std::isfinite(mean)) return unknown;
    if (error.sigmaMs == 0.0) {
        return mean >= window.startMs && mean <= window.endMs ? 1.0 : 0.0;
    }
    if (window.startMs == window.endMs) return 0.0;

    const double lo = detail::standardizedDifference(window.startMs, mean, error.sigmaMs);
    const double hi = detail::standardizedDifference(window.endMs, mean, error.sigmaMs);
    constexpr double inverseSqrtTwo = 0.70710678118654752440084436210485;
    double result;
    // erfc avoids subtracting two CDF values both rounded to one in a tail.
    if (lo >= 0.0) {
        result = 0.5 * (std::erfc(lo * inverseSqrtTwo) - std::erfc(hi * inverseSqrtTwo));
    } else if (hi <= 0.0) {
        result = 0.5 * (std::erfc(-hi * inverseSqrtTwo) - std::erfc(-lo * inverseSqrtTwo));
    } else {
        result = 0.5 * (std::erf(hi * inverseSqrtTwo) - std::erf(lo * inverseSqrtTwo));
    }
    return std::isfinite(result) ? std::clamp(result, 0.0, 1.0) : unknown;
}

struct GaussianTarget {
    bool valid = false;
    double commandMs = unknown;
    double expectedLandingMs = unknown;
    double probability = unknown;
};

// With additive symmetric Gaussian error and fixed ordered boundaries, the
// optimum expected landing is the midpoint. Compensation subtracts the bias;
// positive bias means late arrival. It is NOT a recommendation to retarget an
// existing learned tip/lead pair whose latent bias has not been identified.
[[nodiscard]] inline GaussianTarget optimalGaussianTarget(
    const Interval& window, const GaussianError& error) noexcept
{
    if (!window.valid() || !error.valid()) return {};
    const double midpoint = detail::finiteMidpoint(window.startMs, window.endMs);
    const double command = midpoint - error.biasMs;
    const double probability = gaussianIntervalProbability(window, command, error);
    if (!std::isfinite(probability)) return {};
    return {true, command, command + error.biasMs, probability};
}

// Scheduling feasibility is a separate finite interval. A constrained optimum
// can have very low probability; valid means mathematically defined, not usable
// by a controller. No nearest-frame snapping is performed after optimization.
[[nodiscard]] inline GaussianTarget optimalGaussianTarget(
    const Interval& window, const GaussianError& error, const Interval& commands) noexcept
{
    if (!window.valid() || !error.valid() || !commands.valid()) return {};
    const double midpoint = detail::finiteMidpoint(window.startMs, window.endMs);
    const double idealCommand = midpoint - error.biasMs;
    const double command = std::clamp(idealCommand, commands.startMs, commands.endMs);
    const double probability = gaussianIntervalProbability(window, command, error);
    if (!std::isfinite(probability)) return {};
    return {true, command, command + error.biasMs, probability};
}

template<std::size_t N>
using Covariance = std::array<std::array<double, N>, N>;

struct PropagatedVariance {
    bool valid = false;
    double variance = unknown;
    double sigma = unknown;
};

// Validate in dimensionless correlation coordinates, not against the largest
// raw diagonal: mixed units (milliseconds, percent, percent/ms) otherwise hide
// impossible small-scale covariances behind an unrelated large variance.
// Pivoted Schur complements accept positive SEMI-definite matrices, including
// exact shared errors and exact cancellation; negative variances stay invalid.
// Unrepresentable positive uncertainty is invalid, never a deterministic zero.
template<std::size_t N>
[[nodiscard]] inline PropagatedVariance propagateCovariance(
    const std::array<double, N>& jacobian, const Covariance<N>& covariance) noexcept
{
    static_assert(N > 0, "A variance budget must have at least one component");
    constexpr double tolerance = 128.0 * std::numeric_limits<double>::epsilon()
        * static_cast<double>(N);
    std::array<double, N> deviations{};
    Covariance<N> correlation{};
    for (std::size_t i = 0; i < N; ++i) {
        if (!std::isfinite(jacobian[i]) || !std::isfinite(covariance[i][i])
            || covariance[i][i] < 0.0) return {};
        deviations[i] = std::sqrt(covariance[i][i]);
    }
    for (std::size_t i = 0; i < N; ++i) {
        for (std::size_t j = 0; j < N; ++j) {
            const double value = covariance[i][j];
            if (!std::isfinite(value)) return {};
            if (deviations[i] == 0.0 || deviations[j] == 0.0) {
                // PSD requires an exactly zero row/column for zero variance.
                if (value != 0.0) return {};
                correlation[i][j] = 0.0;
            } else {
                correlation[i][j] = (value / deviations[i]) / deviations[j];
                if (!std::isfinite(correlation[i][j])
                    || std::abs(correlation[i][j]) > 1.0 + tolerance) return {};
            }
        }
    }
    for (std::size_t i = 0; i < N; ++i) {
        for (std::size_t j = i + 1; j < N; ++j) {
            if (std::abs(correlation[i][j] - correlation[j][i]) > tolerance) return {};
            const double symmetric = (correlation[i][j] + correlation[j][i]) * 0.5;
            correlation[i][j] = symmetric;
            correlation[j][i] = symmetric;
        }
    }
    Covariance<N> schur = correlation;
    for (std::size_t k = 0; k < N; ++k) {
        std::size_t pivot = k;
        for (std::size_t i = k + 1; i < N; ++i) {
            if (schur[i][i] > schur[pivot][pivot]) pivot = i;
        }
        if (pivot != k) {
            std::swap(schur[k], schur[pivot]);
            for (std::size_t i = 0; i < N; ++i) std::swap(schur[i][k], schur[i][pivot]);
        }
        const double diagonal = schur[k][k];
        if (!std::isfinite(diagonal) || diagonal < -tolerance) return {};
        if (diagonal <= tolerance) {
            for (std::size_t i = k; i < N; ++i) {
                for (std::size_t j = k; j < N; ++j) {
                    if (!std::isfinite(schur[i][j]) || std::abs(schur[i][j]) > tolerance) return {};
                }
            }
            break;
        }
        for (std::size_t i = k + 1; i < N; ++i) {
            for (std::size_t j = i; j < N; ++j) {
                const double updated = std::fma(-schur[i][k] / diagonal, schur[j][k], schur[i][j]);
                if (!std::isfinite(updated)) return {};
                schur[i][j] = updated;
                schur[j][i] = updated;
            }
        }
    }

    std::array<double, N> weighted{};
    double scale = 0.0;
    for (std::size_t i = 0; i < N; ++i) {
        weighted[i] = jacobian[i] * deviations[i];
        if (!std::isfinite(weighted[i])) return {};
        // A representational underflow is not evidence of deterministic error.
        if (weighted[i] == 0.0 && jacobian[i] != 0.0 && deviations[i] != 0.0) return {};
        scale = std::max(scale, std::abs(weighted[i]));
    }
    if (scale == 0.0) return {true, 0.0, 0.0};
    for (auto& value : weighted) {
        const double normalized = value / scale;
        if (normalized == 0.0 && value != 0.0) return {};
        value = normalized;
    }
    // At most four terms are inserted per matrix entry. The expansion is
    // stack-bounded and has no heap allocation or dependence on live state.
    detail::CompensatedSum<4 * N * N> varianceSum;
    double absoluteTerms = 0.0;
    for (std::size_t i = 0; i < N; ++i) {
        for (std::size_t j = 0; j < N; ++j) {
            // Retain both product roundoffs as well as sum roundoff. Merely
            // compensating the sum still rounds (1-delta)^2 before cancellation
            // and can turn a small positive shared-error residual into zero.
            const double product = weighted[i] * correlation[i][j];
            if (product == 0.0 && weighted[i] != 0.0 && correlation[i][j] != 0.0) return {};
            const double productError = std::fma(weighted[i], correlation[i][j], -product);
            const double term = product * weighted[j];
            if (term == 0.0 && product != 0.0 && weighted[j] != 0.0) return {};
            const double carriedError = productError * weighted[j];
            varianceSum.add(term);
            varianceSum.add(std::fma(product, weighted[j], -term));
            varianceSum.add(carriedError);
            varianceSum.add(std::fma(productError, weighted[j], -carriedError));
            absoluteTerms += std::abs(term);
        }
    }
    const double normalizedVariance = varianceSum.value();
    if (!std::isfinite(normalizedVariance)
        || normalizedVariance < -tolerance * absoluteTerms) return {};
    const double sigma = std::sqrt(std::max(0.0, normalizedVariance)) * scale;
    if (sigma == 0.0 && normalizedVariance > 0.0) return {};
    const double variance = sigma * sigma;
    if (!std::isfinite(sigma) || !std::isfinite(variance)) return {};
    if (variance == 0.0 && sigma != 0.0) return {};
    return {true, variance, sigma};
}

// If correlations are unknown, root-sum-squares silently assumes independence.
// The triangle/Minkowski bound below is valid for all correlations when the
// marginal sigmas themselves are valid. It is a bound, not a Gaussian sigma fit.
template<std::size_t N>
[[nodiscard]] inline double unknownCorrelationSigmaUpperBound(
    const std::array<double, N>& jacobian, const std::array<double, N>& sigmas) noexcept
{
    static_assert(N > 0, "A variance budget must have at least one component");
    double result = 0.0;
    for (std::size_t i = 0; i < N; ++i) {
        if (!std::isfinite(jacobian[i]) || !std::isfinite(sigmas[i]) || sigmas[i] < 0.0) return unknown;
        const double term = std::abs(jacobian[i]) * sigmas[i];
        if (term == 0.0 && jacobian[i] != 0.0 && sigmas[i] != 0.0) return unknown;
        result += term;
        if (!std::isfinite(result)) return unknown;
    }
    return result;
}

struct LinearCrossing {
    bool valid = false;
    double crossingMs = unknown;
    double varianceMs2 = unknown;
    double sigmaMs = unknown;
    // Input order: observation timestamp, fill, velocity, target boundary.
    // Units: ms, percentage points, percentage points/ms, percentage points.
    std::array<double, 4> jacobian{unknown, unknown, unknown, unknown};
};

// First-order delta-method propagation for a local RISING linear trajectory.
// This is not a bound for acceleration, model bias, or a random velocity with
// material mass near zero. The caller must validate that operating regime and
// account for model residual and transport separately, without double counting.
// Past crossings are valid mathematics; this helper grants no scheduling right.
[[nodiscard]] inline LinearCrossing linearCrossing(
    double observationMs, double fillPct, double velocityPctPerMs, double boundaryPct,
    const Covariance<4>& covariance) noexcept
{
    if (!std::isfinite(observationMs) || !std::isfinite(fillPct)
        || !std::isfinite(velocityPctPerMs) || velocityPctPerMs <= 0.0
        || !std::isfinite(boundaryPct)) return {};
    const double horizon = detail::standardizedDifference(boundaryPct, fillPct, velocityPctPerMs);
    const double crossing = observationMs + horizon;
    const std::array<double, 4> jacobian{
        1.0, -1.0 / velocityPctPerMs, -horizon / velocityPctPerMs, 1.0 / velocityPctPerMs};
    if (!std::isfinite(horizon) || !std::isfinite(crossing)) return {};
    const auto propagated = propagateCovariance(jacobian, covariance);
    if (!propagated.valid) return {};
    return {true, crossing, propagated.variance, propagated.sigma, jacobian};
}

} // namespace orion::GreenWindowMath
