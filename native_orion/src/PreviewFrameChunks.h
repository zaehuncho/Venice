#pragma once

#include <QtCore/QByteArray>
#include <QtCore/QByteArrayView>

#include <cstring>
#include <limits>
#include <utility>

namespace orion {

struct PreviewChunkResult {
    bool accepted = false;
    bool complete = false;
    int frameNumber = 0;
    QByteArray jpegBase64;
};

// Strict, in-order reassembly for the sidecar's bounded preview JSONL chunks.
// Telemetry may appear between chunks, but preview chunks themselves are emitted
// sequentially. Any gap, mismatch, oversized value, or older frame drops the
// incomplete image; a newer index-0 frame always replaces it (latest wins).
class PreviewFrameChunkAssembler final {
public:
    static constexpr int kMaxChunkCount = 256;
    static constexpr qsizetype kMaxChunkBytes = 8 * 1024;
    static constexpr qsizetype kMaxFrameBytes = 2 * 1024 * 1024;

    PreviewChunkResult push(int chunkFrameId, int previewFrameNumber,
                            int chunkIndex, int chunkCount, QByteArrayView payload)
    {
        PreviewChunkResult result;
        if (chunkFrameId <= 0 || previewFrameNumber < 0
            || chunkCount <= 0 || chunkCount > kMaxChunkCount
            || chunkIndex < 0 || chunkIndex >= chunkCount || payload.isEmpty()
            || payload.size() > kMaxChunkBytes) {
            clearPending();
            return result;
        }

        if (chunkFrameId != chunkFrameId_) {
            if (chunkFrameId <= lastCompletedFrame_ || chunkFrameId < chunkFrameId_
                || chunkIndex != 0) {
                return result;
            }
            clearPending();
            chunkFrameId_ = chunkFrameId;
            previewFrameNumber_ = previewFrameNumber;
            chunkCount_ = chunkCount;
        }

        if (previewFrameNumber != previewFrameNumber_
            || chunkCount != chunkCount_ || chunkIndex != nextChunk_
            || bytes_.size() > kMaxFrameBytes - payload.size()) {
            clearPending();
            return result;
        }

        bytes_.append(payload.data(), payload.size());
        ++nextChunk_;
        result.accepted = true;
        result.frameNumber = previewFrameNumber;
        if (nextChunk_ == chunkCount_) {
            result.complete = true;
            result.jpegBase64 = std::move(bytes_);
            lastCompletedFrame_ = chunkFrameId;
            clearPending();
        }
        return result;
    }

    void reset()
    {
        clearPending();
        lastCompletedFrame_ = 0;
    }

    [[nodiscard]] int frameNumber() const { return previewFrameNumber_; }
    [[nodiscard]] int nextChunk() const { return nextChunk_; }
    [[nodiscard]] qsizetype bufferedBytes() const { return bytes_.size(); }

private:
    void clearPending()
    {
        chunkFrameId_ = 0;
        previewFrameNumber_ = 0;
        chunkCount_ = 0;
        nextChunk_ = 0;
        bytes_.clear();
    }

    int chunkFrameId_ = 0;
    int previewFrameNumber_ = 0;
    int lastCompletedFrame_ = 0;
    int chunkCount_ = 0;
    int nextChunk_ = 0;
    QByteArray bytes_;
};

// Allocation-free view of the exact compact JSON record emitted by
// autogreen_sidecar._build_frame_chunk_line().  The payload view borrows the
// caller's line and is only valid while that line is alive.
struct PreviewFrameChunkWireRecord {
    bool valid = false;
    int frameNumber = 0;
    int chunkFrameId = 0;
    int chunkIndex = -1;
    int chunkCount = 0;
    QByteArrayView payload;
};

namespace preview_chunk_detail {

inline bool consumeLiteral(QByteArrayView line, qsizetype& pos, const char* literal)
{
    const qsizetype count = static_cast<qsizetype>(std::strlen(literal));
    if (pos < 0 || count < 0 || pos > line.size() - count
        || std::memcmp(line.data() + pos, literal, static_cast<size_t>(count)) != 0) {
        return false;
    }
    pos += count;
    return true;
}

inline bool consumeNonNegativeInt(QByteArrayView line, qsizetype& pos, int& value)
{
    if (pos < 0 || pos >= line.size() || line.data()[pos] < '0' || line.data()[pos] > '9') {
        return false;
    }
    int parsed = 0;
    do {
        const int digit = line.data()[pos] - '0';
        if (parsed > (std::numeric_limits<int>::max() - digit) / 10) {
            return false;
        }
        parsed = parsed * 10 + digit;
        ++pos;
    } while (pos < line.size() && line.data()[pos] >= '0' && line.data()[pos] <= '9');
    value = parsed;
    return true;
}

} // namespace preview_chunk_detail

// Fast parser for the sidecar's fixed-order frame_chunk wire record.  A failed
// parse is intentionally non-destructive: RemotePlaySession falls back to
// QJsonDocument for forward compatibility.  A successful parse avoids one JSON
// DOM plus a base64 bytes -> UTF-16 -> bytes round trip for every 8 KiB chunk
// (often 1,000+ GUI-thread parses per second at 60 FPS).
inline PreviewFrameChunkWireRecord parsePreviewFrameChunkLine(QByteArrayView line)
{
    using namespace preview_chunk_detail;
    PreviewFrameChunkWireRecord record;
    qsizetype pos = 0;
    if (!consumeLiteral(line, pos, "{\"event\":\"frame_chunk\",\"frame_number\":")
        || !consumeNonNegativeInt(line, pos, record.frameNumber)
        || !consumeLiteral(line, pos, ",\"chunk_frame_id\":")
        || !consumeNonNegativeInt(line, pos, record.chunkFrameId)
        || !consumeLiteral(line, pos, ",\"chunk_index\":")
        || !consumeNonNegativeInt(line, pos, record.chunkIndex)
        || !consumeLiteral(line, pos, ",\"chunk_count\":")
        || !consumeNonNegativeInt(line, pos, record.chunkCount)
        || !consumeLiteral(line, pos, ",\"jpeg_b64\":\"")) {
        return record;
    }

    const qsizetype payloadStart = pos;
    while (pos < line.size() && line.data()[pos] != '"') {
        ++pos;
    }
    const qsizetype payloadSize = pos - payloadStart;
    if (payloadSize <= 0 || payloadSize > PreviewFrameChunkAssembler::kMaxChunkBytes
        || !consumeLiteral(line, pos, "\"}")) {
        return record;
    }
    // Tolerate CRLF if a diagnostic relay rewrites the sidecar's LF record.
    if (pos < line.size() && line.data()[pos] == '\r') {
        ++pos;
    }
    if (pos != line.size() || record.chunkFrameId <= 0
        || record.chunkCount <= 0
        || record.chunkCount > PreviewFrameChunkAssembler::kMaxChunkCount
        || record.chunkIndex < 0 || record.chunkIndex >= record.chunkCount) {
        return record;
    }
    record.payload = QByteArrayView(line.data() + payloadStart, payloadSize);
    record.valid = true;
    return record;
}

} // namespace orion
