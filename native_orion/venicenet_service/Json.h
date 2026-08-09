#pragma once

// ───────────────────────────────────────────────────────────────────────────
//  Json.h — tiny, dependency-free JSON for the VeniceNetSvc wire protocol
// ───────────────────────────────────────────────────────────────────────────
//
//  VeniceNetSvc.exe is deliberately Qt-free (it is a Windows service, not a Qt
//  app) so it cannot lean on QJsonDocument the way NetworkBridge.cpp does. This
//  header carries just enough JSON to be byte-compatible with nexus_svc.py's
//  `json.dumps(msg, separators=(",", ":"))` output and to parse the flat
//  command objects the client sends.
//
//  COMPATIBILITY NOTES (load-bearing — do not "tidy"):
//    * Serialisation is COMPACT: no spaces after ':' or ','. Mirrors nexus_svc.
//    * Object key ORDER is preserved by insertion (a std::vector of pairs, not a
//      map). JSON is order-independent for parsing, but keeping insertion order
//      makes the emitted lines diff-identical to nexus_svc for hello/ack/error.
//    * Integers and doubles are distinct kinds so `version`, `buffer_depth`,
//      ports and sizes serialise as `3`, not `3.0`, exactly like the Python
//      side. Doubles round-trip through the shortest representation that keeps
//      three fractional digits (nexus_svc rounds telemetry to 3 places).
//    * Strings escape the JSON-mandatory set only; the protocol never carries
//      control characters beyond that.
//
//  This is NOT a general-purpose JSON library. It handles what the protocol
//  needs (objects, arrays, strings, numbers, bool, null) and rejects the rest.

#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace venicenet {

class JsonValue {
public:
    enum class Kind { Null, Bool, Int, Double, String, Array, Object };

    JsonValue() : kind_(Kind::Null) {}
    explicit JsonValue(bool b) : kind_(Kind::Bool), bool_(b) {}
    explicit JsonValue(int v) : kind_(Kind::Int), int_(v) {}
    explicit JsonValue(std::int64_t v) : kind_(Kind::Int), int_(v) {}
    explicit JsonValue(double v) : kind_(Kind::Double), double_(v) {}
    explicit JsonValue(const char* s) : kind_(Kind::String), str_(s) {}
    explicit JsonValue(std::string s) : kind_(Kind::String), str_(std::move(s)) {}

    static JsonValue makeObject()
    {
        JsonValue v;
        v.kind_ = Kind::Object;
        return v;
    }
    static JsonValue makeArray()
    {
        JsonValue v;
        v.kind_ = Kind::Array;
        return v;
    }

    Kind kind() const { return kind_; }
    bool isObject() const { return kind_ == Kind::Object; }
    bool isArray() const { return kind_ == Kind::Array; }
    bool isString() const { return kind_ == Kind::String; }
    bool isNumber() const { return kind_ == Kind::Int || kind_ == Kind::Double; }
    bool isBool() const { return kind_ == Kind::Bool; }
    bool isNull() const { return kind_ == Kind::Null; }

    bool asBool(bool fallback = false) const
    {
        if (kind_ == Kind::Bool) return bool_;
        return fallback;
    }
    double asDouble(double fallback = 0.0) const
    {
        if (kind_ == Kind::Double) return double_;
        if (kind_ == Kind::Int) return static_cast<double>(int_);
        return fallback;
    }
    std::int64_t asInt(std::int64_t fallback = 0) const
    {
        if (kind_ == Kind::Int) return int_;
        if (kind_ == Kind::Double) return static_cast<std::int64_t>(double_);
        return fallback;
    }
    const std::string& asString() const { return str_; }

    // Object accessors. set() appends (preserving insertion order); a repeated
    // key overwrites in place so the emitted object never has duplicates.
    void set(const std::string& key, JsonValue value)
    {
        for (auto& kv : members_) {
            if (kv.first == key) {
                kv.second = std::move(value);
                return;
            }
        }
        members_.emplace_back(key, std::move(value));
    }
    void setString(const std::string& key, const std::string& value) { set(key, JsonValue(value)); }
    void setBool(const std::string& key, bool value) { set(key, JsonValue(value)); }
    void setInt(const std::string& key, std::int64_t value) { set(key, JsonValue(value)); }
    void setDouble(const std::string& key, double value) { set(key, JsonValue(value)); }

    bool has(const std::string& key) const { return find(key) != nullptr; }
    const JsonValue* find(const std::string& key) const
    {
        for (const auto& kv : members_) {
            if (kv.first == key) return &kv.second;
        }
        return nullptr;
    }
    // Convenience readers used by the verb dispatch. Return the fallback when the
    // key is absent OR the wrong type, matching QJsonObject::value(...).toX().
    std::string getString(const std::string& key, const std::string& fallback = "") const
    {
        const JsonValue* v = find(key);
        return (v && v->isString()) ? v->asString() : fallback;
    }
    bool getBool(const std::string& key, bool fallback = false) const
    {
        const JsonValue* v = find(key);
        return (v && v->isBool()) ? v->asBool() : fallback;
    }
    bool hasNumber(const std::string& key) const
    {
        const JsonValue* v = find(key);
        return v && v->isNumber();
    }
    double getNumber(const std::string& key, double fallback = 0.0) const
    {
        const JsonValue* v = find(key);
        return (v && v->isNumber()) ? v->asDouble() : fallback;
    }

    void append(JsonValue value) { elements_.push_back(std::move(value)); }
    const std::vector<JsonValue>& elements() const { return elements_; }
    const std::vector<std::pair<std::string, JsonValue>>& members() const { return members_; }

    std::string serialize() const
    {
        std::string out;
        serializeInto(out);
        return out;
    }

    // Parse a JSON document. Returns true on success. On failure `out` is left
    // as Null. Only what the protocol needs is accepted.
    static bool parse(const std::string& text, JsonValue& out)
    {
        size_t pos = 0;
        skipWs(text, pos);
        JsonValue value;
        if (!parseValue(text, pos, value)) {
            out = JsonValue();
            return false;
        }
        skipWs(text, pos);
        if (pos != text.size()) {
            out = JsonValue();
            return false; // trailing garbage
        }
        out = std::move(value);
        return true;
    }

private:
    Kind kind_ = Kind::Null;
    bool bool_ = false;
    std::int64_t int_ = 0;
    double double_ = 0.0;
    std::string str_;
    std::vector<JsonValue> elements_;
    std::vector<std::pair<std::string, JsonValue>> members_;

    static void escapeInto(const std::string& s, std::string& out)
    {
        out.push_back('"');
        for (const char c : s) {
            switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x",
                                  static_cast<unsigned>(static_cast<unsigned char>(c)));
                    out += buf;
                } else {
                    out.push_back(c);
                }
            }
        }
        out.push_back('"');
    }

    static void formatDouble(double v, std::string& out)
    {
        if (!std::isfinite(v)) {
            // Non-finite cannot appear in JSON; the delay path clamps before it
            // ever reaches here, but emit 0 rather than an invalid token.
            out += "0";
            return;
        }
        // Shortest representation that keeps up to 3 fractional digits, matching
        // nexus_svc's round(x, 3). Trim trailing zeros and a dangling '.'.
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%.3f", v);
        std::string s(buf);
        if (s.find('.') != std::string::npos) {
            size_t last = s.find_last_not_of('0');
            if (s[last] == '.') --last;
            s.erase(last + 1);
        }
        out += s;
    }

    void serializeInto(std::string& out) const
    {
        switch (kind_) {
        case Kind::Null: out += "null"; break;
        case Kind::Bool: out += (bool_ ? "true" : "false"); break;
        case Kind::Int: out += std::to_string(int_); break;
        case Kind::Double: formatDouble(double_, out); break;
        case Kind::String: escapeInto(str_, out); break;
        case Kind::Array: {
            out.push_back('[');
            bool first = true;
            for (const auto& e : elements_) {
                if (!first) out.push_back(',');
                first = false;
                e.serializeInto(out);
            }
            out.push_back(']');
            break;
        }
        case Kind::Object: {
            out.push_back('{');
            bool first = true;
            for (const auto& kv : members_) {
                if (!first) out.push_back(',');
                first = false;
                escapeInto(kv.first, out);
                out.push_back(':');
                kv.second.serializeInto(out);
            }
            out.push_back('}');
            break;
        }
        }
    }

    static void skipWs(const std::string& t, size_t& pos)
    {
        while (pos < t.size()) {
            const char c = t[pos];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                ++pos;
            } else {
                break;
            }
        }
    }

    static bool parseValue(const std::string& t, size_t& pos, JsonValue& out)
    {
        skipWs(t, pos);
        if (pos >= t.size()) return false;
        const char c = t[pos];
        switch (c) {
        case '{': return parseObject(t, pos, out);
        case '[': return parseArray(t, pos, out);
        case '"': {
            std::string s;
            if (!parseString(t, pos, s)) return false;
            out = JsonValue(std::move(s));
            return true;
        }
        case 't':
        case 'f': return parseBool(t, pos, out);
        case 'n': return parseNull(t, pos, out);
        default: return parseNumber(t, pos, out);
        }
    }

    static bool parseObject(const std::string& t, size_t& pos, JsonValue& out)
    {
        out = makeObject();
        ++pos; // '{'
        skipWs(t, pos);
        if (pos < t.size() && t[pos] == '}') {
            ++pos;
            return true;
        }
        while (true) {
            skipWs(t, pos);
            if (pos >= t.size() || t[pos] != '"') return false;
            std::string key;
            if (!parseString(t, pos, key)) return false;
            skipWs(t, pos);
            if (pos >= t.size() || t[pos] != ':') return false;
            ++pos;
            JsonValue value;
            if (!parseValue(t, pos, value)) return false;
            out.set(key, std::move(value));
            skipWs(t, pos);
            if (pos >= t.size()) return false;
            if (t[pos] == ',') {
                ++pos;
                continue;
            }
            if (t[pos] == '}') {
                ++pos;
                return true;
            }
            return false;
        }
    }

    static bool parseArray(const std::string& t, size_t& pos, JsonValue& out)
    {
        out = makeArray();
        ++pos; // '['
        skipWs(t, pos);
        if (pos < t.size() && t[pos] == ']') {
            ++pos;
            return true;
        }
        while (true) {
            JsonValue value;
            if (!parseValue(t, pos, value)) return false;
            out.append(std::move(value));
            skipWs(t, pos);
            if (pos >= t.size()) return false;
            if (t[pos] == ',') {
                ++pos;
                continue;
            }
            if (t[pos] == ']') {
                ++pos;
                return true;
            }
            return false;
        }
    }

    static bool parseString(const std::string& t, size_t& pos, std::string& out)
    {
        out.clear();
        ++pos; // opening quote
        while (pos < t.size()) {
            const char c = t[pos++];
            if (c == '"') return true;
            if (c == '\\') {
                if (pos >= t.size()) return false;
                const char e = t[pos++];
                switch (e) {
                case '"': out.push_back('"'); break;
                case '\\': out.push_back('\\'); break;
                case '/': out.push_back('/'); break;
                case 'b': out.push_back('\b'); break;
                case 'f': out.push_back('\f'); break;
                case 'n': out.push_back('\n'); break;
                case 'r': out.push_back('\r'); break;
                case 't': out.push_back('\t'); break;
                case 'u': {
                    if (pos + 4 > t.size()) return false;
                    unsigned code = 0;
                    for (int i = 0; i < 4; ++i) {
                        const char h = t[pos++];
                        code <<= 4;
                        if (h >= '0' && h <= '9') code |= static_cast<unsigned>(h - '0');
                        else if (h >= 'a' && h <= 'f') code |= static_cast<unsigned>(h - 'a' + 10);
                        else if (h >= 'A' && h <= 'F') code |= static_cast<unsigned>(h - 'A' + 10);
                        else return false;
                    }
                    // Minimal UTF-8 encoding of the BMP code point. Surrogate
                    // pairs are not needed by this protocol (ASCII verbs/tokens).
                    if (code < 0x80) {
                        out.push_back(static_cast<char>(code));
                    } else if (code < 0x800) {
                        out.push_back(static_cast<char>(0xC0 | (code >> 6)));
                        out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
                    } else {
                        out.push_back(static_cast<char>(0xE0 | (code >> 12)));
                        out.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3F)));
                        out.push_back(static_cast<char>(0x80 | (code & 0x3F)));
                    }
                    break;
                }
                default: return false;
                }
            } else {
                out.push_back(c);
            }
        }
        return false; // unterminated
    }

    static bool parseBool(const std::string& t, size_t& pos, JsonValue& out)
    {
        if (t.compare(pos, 4, "true") == 0) {
            pos += 4;
            out = JsonValue(true);
            return true;
        }
        if (t.compare(pos, 5, "false") == 0) {
            pos += 5;
            out = JsonValue(false);
            return true;
        }
        return false;
    }

    static bool parseNull(const std::string& t, size_t& pos, JsonValue& out)
    {
        if (t.compare(pos, 4, "null") == 0) {
            pos += 4;
            out = JsonValue();
            return true;
        }
        return false;
    }

    static bool parseNumber(const std::string& t, size_t& pos, JsonValue& out)
    {
        const size_t start = pos;
        bool isDouble = false;
        if (pos < t.size() && (t[pos] == '-' || t[pos] == '+')) ++pos;
        while (pos < t.size()) {
            const char c = t[pos];
            if (c >= '0' && c <= '9') {
                ++pos;
            } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                isDouble = true;
                ++pos;
            } else {
                break;
            }
        }
        if (pos == start) return false;
        const std::string token = t.substr(start, pos - start);
        try {
            if (isDouble) {
                out = JsonValue(std::stod(token));
            } else {
                out = JsonValue(static_cast<std::int64_t>(std::stoll(token)));
            }
        } catch (...) {
            return false;
        }
        return true;
    }
};

} // namespace venicenet
