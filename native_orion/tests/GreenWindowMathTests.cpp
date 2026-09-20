#include "../src/GreenWindowMath.h"

#include <cstdlib>
#include <iostream>
#include <random>

using namespace orion::GreenWindowMath;

namespace {
int checks = 0;
int failures = 0;
void check(bool condition, const char* label)
{
    ++checks;
    if (!condition) {
        ++failures;
        std::cerr << "FAIL " << label << '\n';
    }
}
bool close(double a, double b, double relative = 1e-11, double absolute = 1e-12)
{
    return std::isfinite(a) && std::isfinite(b)
        && std::abs(a - b) <= absolute + relative * std::max(std::abs(a), std::abs(b));
}

void probabilityCases()
{
    const Interval window{100.0, 108.0};
    const GaussianError zeroBias{0.0, 2.0};
    const auto optimum = optimalGaussianTarget(window, zeroBias);
    check(optimum.valid && optimum.commandMs == 104.0, "8ms midpoint");
    check(close(optimum.probability, 0.9544997361036416), "8ms sigma2 nominal probability");
    check(close(gaussianIntervalProbability(window, 108.0, zeroBias), 0.4999683287581669), "tip boundary probability");
    const auto biased = optimalGaussianTarget(window, {1.5, 2.0});
    check(biased.valid && biased.commandMs == 102.5 && biased.expectedLandingMs == 104.0, "late bias subtracts");
    check(close(biased.probability, optimum.probability), "bias compensated probability");
    const auto constrained = optimalGaussianTarget(window, zeroBias, {106.0, 110.0});
    check(constrained.valid && constrained.commandMs == 106.0, "constrained best command");
    check(constrained.probability < optimum.probability, "constraint reduces probability");
    for (int i = 0; i <= 1000; ++i) {
        const double command = 90.0 + i * 0.03;
        check(gaussianIntervalProbability(window, command, zeroBias) <= optimum.probability + 1e-14,
              "midpoint beats command sweep");
    }
    check(gaussianIntervalProbability(window, 100.0, {0.0, 0.0}) == 1.0, "deterministic start inclusive");
    check(gaussianIntervalProbability(window, 108.0, {0.0, 0.0}) == 1.0, "deterministic end inclusive");
    check(gaussianIntervalProbability(window, 108.001, {0.0, 0.0}) == 0.0, "deterministic outside");
    check(gaussianIntervalProbability({3.0, 3.0}, 3.0, {0.0, 0.0}) == 1.0, "deterministic point window");
    check(gaussianIntervalProbability({3.0, 3.0}, 3.0, {0.0, 1.0}) == 0.0, "continuous point window");
    check(!optimalGaussianTarget({}, zeroBias).valid, "unknown window invalid");
    check(!optimalGaussianTarget({108.0, 100.0}, zeroBias).valid, "reversed window invalid");
    check(!optimalGaussianTarget(window, {}).valid, "unknown error invalid");
    check(!optimalGaussianTarget(window, {0.0, -1.0}).valid, "negative sigma invalid");
    check(!optimalGaussianTarget(window, zeroBias, {10.0, 9.0}).valid, "reversed commands invalid");
    check(std::isnan(gaussianIntervalProbability(window, unknown, zeroBias)), "unknown command not zero");
    const double tailRight = gaussianIntervalProbability({10.0, 11.0}, 0.0, {0.0, 1.0});
    const double tailLeft = gaussianIntervalProbability({-11.0, -10.0}, 0.0, {0.0, 1.0});
    check(tailRight > 7e-24 && tailRight < 8e-24, "nonzero extreme tail");
    check(close(tailRight, tailLeft, 1e-13, 0.0), "symmetric tails");
    const double maximum = std::numeric_limits<double>::max();
    const auto enormous = optimalGaussianTarget({-maximum, maximum}, {0.0, maximum});
    check(enormous.valid && enormous.commandMs == 0.0, "finite midpoint no overflow");
    check(close(enormous.probability, 0.6826894921370859), "large scale central probability");
    check(close(gaussianIntervalProbability({0.0, maximum}, -maximum, {0.0, maximum}),
                0.13590512198327787), "standardized difference no overflow");
    check(!optimalGaussianTarget({maximum, maximum}, {-maximum, 1.0}).valid, "unrepresentable command invalid");
}

void covarianceCases()
{
    const std::array<double, 2> sum{1.0, 1.0};
    const Covariance<2> independent{{{4.0, 0.0}, {0.0, 9.0}}};
    const auto independentResult = propagateCovariance(sum, independent);
    check(independentResult.valid && close(independentResult.variance, 13.0), "independent budget");
    const Covariance<2> correlated{{{4.0, 6.0}, {6.0, 9.0}}};
    const auto positive = propagateCovariance(sum, correlated);
    check(positive.valid && close(positive.sigma, 5.0), "positive shared error not RSS");
    const auto cancellation = propagateCovariance(std::array<double, 2>{3.0, -2.0}, correlated);
    check(cancellation.valid && close(cancellation.variance, 0.0), "shared common-mode cancellation");
    const Covariance<2> negative{{{4.0, -6.0}, {-6.0, 9.0}}};
    const auto negativeResult = propagateCovariance(sum, negative);
    check(negativeResult.valid && close(negativeResult.sigma, 1.0), "negative correlated error");
    check(close(unknownCorrelationSigmaUpperBound(sum, std::array<double, 2>{2.0, 3.0}), 5.0),
          "unknown correlation bound");
    check(std::isnan(unknownCorrelationSigmaUpperBound(sum, std::array<double, 2>{2.0, unknown})),
          "unknown marginal never zero");
    check(!propagateCovariance(sum, Covariance<2>{{{4.0, 7.0}, {7.0, 9.0}}}).valid, "non-PSD invalid");
    check(!propagateCovariance(sum, Covariance<2>{{{4.0, 1.0}, {2.0, 9.0}}}).valid, "asymmetry invalid");
    check(!propagateCovariance(sum, Covariance<2>{{{-1e-30, 0.0}, {0.0, 1e30}}}).valid, "tiny negative variance invalid");
    check(!propagateCovariance(sum, Covariance<2>{{{0.0, 1e-30}, {1e-30, 1e30}}}).valid, "zero variance covariance invalid");
    check(!propagateCovariance(sum, Covariance<2>{{{4.0, unknown}, {unknown, 9.0}}}).valid, "unknown covariance invalid");
    check(!propagateCovariance(std::array<double, 2>{1.0, unknown}, independent).valid, "unknown derivative invalid");
    const Covariance<3> pairwiseButNotPsd{{{1.0, -0.9, -0.9}, {-0.9, 1.0, -0.9}, {-0.9, -0.9, 1.0}}};
    check(!propagateCovariance(std::array<double, 3>{1.0, 1.0, 1.0}, pairwiseButNotPsd).valid,
          "pairwise correlation bounds insufficient");
    const Covariance<3> mixedScaleBad{{{1e24, 0.0, 0.0}, {0.0, 1e-24, 2e-24}, {0.0, 2e-24, 1e-24}}};
    check(!propagateCovariance(std::array<double, 3>{1.0, 1.0, 1.0}, mixedScaleBad).valid,
          "large variance cannot hide invalid tiny block");
    const auto zero = propagateCovariance(sum, Covariance<2>{});
    check(zero.valid && zero.variance == 0.0 && zero.sigma == 0.0, "zero covariance is deterministic");
    const Covariance<2> rescaled{{{4e-12, 6.0}, {6.0, 9e12}}};
    const auto rescaledResult = propagateCovariance(std::array<double, 2>{1e6, 1e-6}, rescaled);
    check(rescaledResult.valid && close(rescaledResult.sigma, 5.0), "units rescale invariant");
}

void crossingCases()
{
    Covariance<4> covariance{};
    covariance[0][0] = 1.0;
    covariance[1][1] = 0.04;
    covariance[2][2] = 0.000001;
    covariance[3][3] = 0.01;
    const auto crossing = linearCrossing(1000.0, 80.0, 0.2, 100.0, covariance);
    check(crossing.valid && close(crossing.crossingMs, 1100.0), "subframe linear crossing");
    check(close(crossing.varianceMs2, 2.5), "crossing covariance propagated");
    check(close(crossing.jacobian[0], 1.0) && close(crossing.jacobian[1], -5.0)
          && close(crossing.jacobian[2], -500.0) && close(crossing.jacobian[3], 5.0), "crossing analytic gradient");
    Covariance<4> commonGeometry{};
    commonGeometry[1][1] = commonGeometry[1][3] = commonGeometry[3][1] = commonGeometry[3][3] = 0.25;
    const auto common = linearCrossing(1000.0, 80.0, 0.2, 100.0, commonGeometry);
    check(common.valid && close(common.sigmaMs, 0.0), "shared coordinate offset cancels in gap");
    check(!linearCrossing(1000.0, 80.0, 0.0, 100.0, covariance).valid, "flat velocity invalid");
    check(!linearCrossing(1000.0, 80.0, -0.2, 100.0, covariance).valid, "descending velocity invalid");
    check(!linearCrossing(1000.0, unknown, 0.2, 100.0, covariance).valid, "unknown fill invalid");
    const auto past = linearCrossing(1000.0, 90.0, 0.2, 80.0, covariance);
    check(past.valid && close(past.crossingMs, 950.0), "past crossing mathematical not scheduling authority");
    const auto ageA = linearCrossing(1000.0, 80.0, 0.2, 100.0, covariance);
    const auto ageB = linearCrossing(1010.0, 82.0, 0.2, 100.0, covariance);
    check(ageA.valid && ageB.valid && close(ageA.crossingMs, ageB.crossingMs), "observation-time propagation invariant");
    std::array<double, 4> values{1000.0, 80.0, 0.2, 100.0};
    for (std::size_t i = 0; i < 4; ++i) {
        const double h = i == 2 ? 1e-7 : 1e-5;
        auto plus = values;
        auto minus = values;
        plus[i] += h;
        minus[i] -= h;
        const auto p = linearCrossing(plus[0], plus[1], plus[2], plus[3], covariance);
        const auto m = linearCrossing(minus[0], minus[1], minus[2], minus[3], covariance);
        check(p.valid && m.valid && close((p.crossingMs - m.crossingMs) / (2.0 * h), crossing.jacobian[i], 1e-7),
              "analytic gradient matches finite difference");
    }
}

void seededPsdPropertyCases()
{
    std::mt19937_64 random(20260918);
    std::uniform_real_distribution<double> uniform(-2.0, 2.0);
    const std::array<double, 4> ordinaryUnits{1e-6, 1.0, 1e3, 1e6};
    const std::array<double, 4> extremeUnits{
        std::ldexp(1.0, -400), std::ldexp(1.0, -150),
        std::ldexp(1.0, 150), std::ldexp(1.0, 400)};
    for (int sample = 0; sample < 4000; ++sample) {
        const auto& units = sample < 2000 ? ordinaryUnits : extremeUnits;
        Covariance<4> factor{};
        Covariance<4> covariance{};
        std::array<double, 4> jacobian{};
        const std::size_t rank = static_cast<std::size_t>(sample % 4) + 1;
        for (std::size_t i = 0; i < 4; ++i) {
            jacobian[i] = uniform(random) / units[i];
            for (std::size_t k = 0; k < rank; ++k) factor[i][k] = uniform(random) * units[i];
        }
        for (std::size_t i = 0; i < 4; ++i) {
            for (std::size_t j = 0; j < 4; ++j) {
                for (std::size_t k = 0; k < rank; ++k) covariance[i][j] += factor[i][k] * factor[j][k];
            }
        }
        double reference = 0.0;
        for (std::size_t k = 0; k < rank; ++k) {
            double component = 0.0;
            for (std::size_t i = 0; i < 4; ++i) component += jacobian[i] * factor[i][k];
            reference += component * component;
        }
        const auto result = propagateCovariance(jacobian, covariance);
        check(result.valid, "generated PSD accepted");
        check(result.valid && close(result.variance, reference, 1e-9, 1e-11), "generated PSD propagation matches factor sum");
        Covariance<4> permuted{};
        std::array<double, 4> permutedJacobian{};
        for (std::size_t i = 0; i < 4; ++i) {
            const std::size_t sourceI = (i + static_cast<std::size_t>(sample)) % 4;
            permutedJacobian[i] = jacobian[sourceI];
            for (std::size_t j = 0; j < 4; ++j) {
                const std::size_t sourceJ = (j + static_cast<std::size_t>(sample)) % 4;
                permuted[i][j] = covariance[sourceI][sourceJ];
            }
        }
        const auto reordered = propagateCovariance(permutedJacobian, permuted);
        check(reordered.valid && close(reordered.variance, reference, 1e-9, 1e-11),
              "PSD propagation independent of variable ordering");
    }
}

void numericalEdgeCases()
{
    const Covariance<2> shared{{{1.0, 1.0}, {1.0, 1.0}}};
    const Covariance<2> opposite{{{1.0, -1.0}, {-1.0, 1.0}}};
    const Covariance<4> sharedFour{{{1.0, 1.0, 1.0, 1.0}, {1.0, 1.0, 1.0, 1.0},
                                   {1.0, 1.0, 1.0, 1.0}, {1.0, 1.0, 1.0, 1.0}}};
    // A small residual of common-mode error is not a deterministic zero. Use
    // exactly representable binary increments down to one ulp below one.
    for (int exponent = 4; exponent <= 53; ++exponent) {
        const double delta = std::ldexp(1.0, -exponent);
        const double expected = delta * delta;
        const auto a = propagateCovariance(std::array<double, 2>{1.0, -1.0 + delta}, shared);
        const auto b = propagateCovariance(std::array<double, 2>{1.0 - delta, -1.0}, shared);
        const auto c = propagateCovariance(std::array<double, 2>{1.0, 1.0 - delta}, opposite);
        check(a.valid && close(a.variance, expected, 1e-11, 0.0), "small shared residual retained");
        check(b.valid && close(b.variance, expected, 1e-11, 0.0), "small shared residual order invariant");
        check(c.valid && close(c.variance, expected, 1e-11, 0.0), "small opposite residual retained");
        for (std::size_t coordinate = 0; coordinate < 4; ++coordinate) {
            std::array<double, 4> jacobian{1.0, -1.0, 1.0, -1.0};
            jacobian[coordinate] -= std::copysign(delta, jacobian[coordinate]);
            const auto four = propagateCovariance(jacobian, sharedFour);
            check(four.valid && close(four.variance, expected, 1e-11, 0.0),
                  "four component residual order invariant");
        }
    }
    const double tiny = std::numeric_limits<double>::denorm_min();
    const double maximum = std::numeric_limits<double>::max();
    check(!propagateCovariance(std::array<double, 1>{0.1}, Covariance<1>{{{tiny}}}).valid,
          "unrepresentable positive variance is not deterministic");
    check(!propagateCovariance(std::array<double, 1>{tiny}, Covariance<1>{{{tiny}}}).valid,
          "underflowed positive weighted sigma is not deterministic");
    const Covariance<2> almostOpposite{{{1.0, -1.0 + std::numeric_limits<double>::epsilon()},
                                       {-1.0 + std::numeric_limits<double>::epsilon(), 1.0}}};
    check(!propagateCovariance(std::array<double, 2>{tiny, tiny}, almostOpposite).valid,
          "underflowed residual sigma is not deterministic");
    check(std::isnan(unknownCorrelationSigmaUpperBound(std::array<double, 1>{tiny},
                                                     std::array<double, 1>{tiny})),
          "underflowed positive bound is not zero");
    const auto extremeCrossing = linearCrossing(0.0, -maximum, maximum, maximum, Covariance<4>{});
    check(extremeCrossing.valid && extremeCrossing.crossingMs == 2.0,
          "finite crossing quotient survives intermediate difference overflow");
    check(extremeCrossing.valid && extremeCrossing.varianceMs2 == 0.0,
          "extreme deterministic crossing retains explicit zero covariance");
    const double infinity = std::numeric_limits<double>::infinity();
    check(std::isnan(gaussianIntervalProbability({0.0, infinity}, 0.0, {0.0, 1.0})),
          "infinite interval endpoint invalid");
    check(std::isnan(gaussianIntervalProbability({0.0, 1.0}, 0.0, {0.0, infinity})),
          "infinite sigma invalid");
    check(std::isnan(gaussianIntervalProbability({0.0, 1.0}, 0.0, {infinity, 1.0})),
          "infinite bias invalid");
    check(!propagateCovariance(std::array<double, 1>{1.0}, Covariance<1>{{{infinity}}}).valid,
          "infinite covariance invalid");
    check(!propagateCovariance(std::array<double, 1>{maximum}, Covariance<1>{{{maximum}}}).valid,
          "unrepresentable weighted variance invalid");
    check(!propagateCovariance(std::array<double, 1>{0.0}, Covariance<1>{{{unknown}}}).valid,
          "zero sensitivity cannot turn unknown covariance into evidence");
}
} // namespace

int main()
{
    probabilityCases();
    covarianceCases();
    crossingCases();
    seededPsdPropertyCases();
    numericalEdgeCases();
    std::cout << "GreenWindowMath checks=" << checks << " failures=" << failures
              << " seed=20260918 model=nominal-fixed-window-not-calibrated\n";
    return failures == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
