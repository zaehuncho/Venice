#pragma once

#include <QtCore/QObject>
#include <QtCore/QPointer>
#include <QtCore/QProcess>
#include <QtCore/QTimer>
#include <functional>
#include <utility>

namespace orion {

// Own one retired process generation. The GUI never waits for process exit.
// Child cleanup is supplied by the owner (a generation-specific Windows job).
class AsyncProcessRetirer final : public QObject {
public:
    struct TerminationTrace {
        enum Kind { GracefulRequested, ForcedRequested, ObservedExit } kind;
        qint64 pid = 0;
        bool forced = false;
        int exitCode = 0;
        QProcess::ExitStatus exitStatus = QProcess::NormalExit;
    };

    AsyncProcessRetirer(QProcess* process, QObject* parent,
                        std::function<void()> releaseChildren,
                        std::function<void(bool)> completed,
                        std::function<void(QProcess*)> claimProcess = {},
                        std::function<void(const TerminationTrace&)> terminationTrace = {})
        : QObject(parent), process_(process), releaseChildren_(std::move(releaseChildren)),
          completed_(std::move(completed)), claimProcess_(std::move(claimProcess)),
          terminationTrace_(std::move(terminationTrace))
    {
        if (process_) {
            process_->disconnect();
            process_->setParent(this);
            connect(process_, &QProcess::finished, this,
                    [this](int code, QProcess::ExitStatus status) { finish(code, status); });
            connect(process_, &QProcess::errorOccurred, this,
                    [this](QProcess::ProcessError error) {
                        if (error == QProcess::FailedToStart) finish();
                    });
            // A verbose shutdown must not deadlock on a full stdout/stderr pipe.
            connect(process_, &QProcess::readyReadStandardOutput, this,
                    [this]() { process_->readAllStandardOutput(); });
            connect(process_, &QProcess::readyReadStandardError, this,
                    [this]() { process_->readAllStandardError(); });
            connect(process_, &QProcess::started, this, [this]() { requestShutdown(); });
        }
        grace_.setSingleShot(true);
        connect(&grace_, &QTimer::timeout, this, [this]() { forceStop(); });
    }

    ~AsyncProcessRetirer() override
    {
        completed_ = {};
        if (process_) {
            process_->disconnect();
            if (process_->state() != QProcess::NotRunning) {
                trace(TerminationTrace::ForcedRequested);
                claimProcess();
                releaseChildren();
                process_->kill();
                process_->waitForFinished(500); // final object destruction only
            }
        }
        releaseChildren();
    }

    void cancelCompletion() { completed_ = {}; }

    void start(int graceMs)
    {
        grace_.start(qMax(1, graceMs));
        // Always complete on a later event turn, including already-exited children.
        QTimer::singleShot(0, this, [this]() {
            if (!process_ || process_->state() == QProcess::NotRunning) finish();
            else if (process_->state() == QProcess::Running) requestShutdown();
        });
    }

    // Only application destruction uses this; normal buttons use start().
    void waitForExit(int graceMs)
    {
        if (done_) return;
        if (process_ && process_->state() == QProcess::Starting)
            process_->waitForStarted(qMax(1, graceMs));
        requestShutdown();
        if (process_ && process_->state() != QProcess::NotRunning
                && !process_->waitForFinished(qMax(1, graceMs))) {
            forceStop();
            process_->waitForFinished(500);
        }
        if (!process_ || process_->state() == QProcess::NotRunning) finish();
    }

private:
    void requestShutdown()
    {
        if (done_ || shutdownSent_ || !process_ || process_->state() != QProcess::Running) return;
        claimProcess();
        shutdownSent_ = true;
        trace(TerminationTrace::GracefulRequested);
        process_->write("{\"cmd\":\"shutdown\"}\n");
        process_->closeWriteChannel();
        // Do not send WM_CLOSE here: let the sidecar close its console session first.
    }
    void claimProcess()
    {
        // Cancellation can retire a QProcess while it is still Starting. Its
        // old started-handler is detached, so claim that generation here once a
        // PID exists, before shutdown or a deadline kill can leave descendants.
        if (process_ && process_->processId() != 0)
            if (auto claim = std::exchange(claimProcess_, {})) claim(process_);
    }
    void releaseChildren()
    {
        if (auto release = std::exchange(releaseChildren_, {})) release();
    }
    void forceStop()
    {
        if (done_) return;
        forced_ = true;
        claimProcess();
        trace(TerminationTrace::ForcedRequested);
        releaseChildren();
        if (process_ && process_->state() != QProcess::NotRunning) process_->kill();
        else finish();
    }
    void trace(TerminationTrace::Kind kind, int exitCode = 0,
               QProcess::ExitStatus status = QProcess::NormalExit)
    {
        if (process_ && process_->processId() != 0)
            pid_ = static_cast<qint64>(process_->processId());
        if (terminationTrace_)
            terminationTrace_({kind, pid_, forced_, exitCode, status});
    }
    void finish(int exitCode = 0,
                QProcess::ExitStatus status = QProcess::NormalExit)
    {
        if (done_) return;
        done_ = true;
        grace_.stop();
        if (process_ && process_->state() == QProcess::NotRunning) {
            exitCode = process_->exitCode();
            status = process_->exitStatus();
        }
        trace(TerminationTrace::ObservedExit, exitCode, status);
        releaseChildren();
        auto completed = std::exchange(completed_, {});
        deleteLater();
        if (completed) completed(forced_);
    }

    QPointer<QProcess> process_;
    QTimer grace_;
    std::function<void()> releaseChildren_;
    std::function<void(bool)> completed_;
    std::function<void(QProcess*)> claimProcess_;
    std::function<void(const TerminationTrace&)> terminationTrace_;
    qint64 pid_ = 0;
    bool shutdownSent_ = false;
    bool forced_ = false;
    bool done_ = false;
};
} // namespace orion
