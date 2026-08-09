#pragma once

namespace orion::remote_play_decode {

// Hardware decode and zero-copy are one capability pair. A persisted legacy
// toggle must never enable only one half: software-decoded CPU frames require
// zero-copy off, while an explicit hardware diagnostic may use zero-copy only
// with the Vulkan renderer.
struct Policy final {
    bool hardwareDecode = false;
    bool zeroCopy = false;
};

[[nodiscard]] constexpr Policy resolve(bool explicitHardwareOptIn,
                                       bool vulkanRenderer) noexcept
{
    return Policy{
        explicitHardwareOptIn,
        explicitHardwareOptIn && vulkanRenderer,
    };
}

} // namespace orion::remote_play_decode
