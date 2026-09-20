#pragma once

#include <limits>
#include <vector>

namespace orion {

// Human-observed makes/misses only. Deliberately independent of release ids,
// meter self-grades and all timing/learning state. Reconnects do not erase a
// batch; the user resets it explicitly or starts a new application session.
class ManualShotTally final {
public:
    [[nodiscard]] int total() const noexcept { return static_cast<int>(history_.size()); }
    [[nodiscard]] int makes() const noexcept { return makes_; }
    [[nodiscard]] int misses() const noexcept { return total() - makes_; }

    bool record(bool made)
    {
        if (history_.size() >= static_cast<std::size_t>(std::numeric_limits<int>::max())) {
            return false;
        }
        history_.push_back(made);
        makes_ += made ? 1 : 0;
        return true;
    }

    bool undo()
    {
        if (history_.empty()) {
            return false;
        }
        makes_ -= history_.back() ? 1 : 0;
        history_.pop_back();
        return true;
    }

    bool reset()
    {
        if (history_.empty()) {
            return false;
        }
        history_.clear();
        makes_ = 0;
        return true;
    }

private:
    std::vector<bool> history_;
    int makes_ = 0;
};

} // namespace orion
