/**
 * Bounded Multi-Producer Multi-Consumer Queue
 * Modern Implementation: C++20 std::atomic_wait/notify
 * 
 * This implementation leverages C++20's new atomic wait/notify primitives,
 * which provide a futex-like interface directly on atomic variables.
 * 
 * Key advantages:
 * + Can avoid kernel transitions via spin-wait before blocking
 * + Tighter integration with hardware (x86 WAITPKG, ARM WFE/SEV, RISC-V Zawrs)
 * + More efficient wake-ups under contention
 * + Lock-free fast paths possible
 * 
 * Hardware primitives used (when available):
 * - x86: UMONITOR/UMWAIT/TPAUSE (WAITPKG extension)
 * - ARM: WFE (Wait For Event) / SEV (Send Event)
 * - RISC-V: Zawrs extension (WRS.NTO/WRS.STO)
 * - Fallback: OS futex (Linux futex_waitv, Windows WaitOnAddress)
 */

#include <iostream>
#include <vector>
#include <thread>
#include <atomic>
#include <chrono>
#include <random>
#include <iomanip>
#include <array>
#include <mutex>

// Hybrid design: data protected by a small mutex, sleeping done via atomic_wait
// rather than condition_variable. This removes most lock-free complexity while
// still showcasing atomic_wait/notify for parking threads efficiently.
template<typename T, size_t Capacity>
class MPMCQueueAtomicWait {
private:
    // Circular buffer storage (manually managed indices under mutex)
    std::array<T, Capacity> buffer_{};
    size_t head_ = 0; // next position to deq
    size_t tail_ = 0; // next position to enq
    size_t count_ = 0; // number of elements currently stored

    mutable std::mutex data_mutex_;
    std::atomic<bool> shutdown_{false};

    // Generation counters emulate condition variables
    // Basically they track the number of times the queue has been modified
    // (enqueued or dequeued), allowing threads to wait for changes.

    // Atomic generation counters padded to avoid false sharing
    // Not-full generation (producers wait on this when full)
    alignas(64) std::atomic<uint64_t> not_full_gen_{0};
    // Not-empty generation (consumers wait on this when empty)
    alignas(64) std::atomic<uint64_t> not_empty_gen_{0};

public:
    MPMCQueueAtomicWait() = default;

    bool enq(T value) {
        // take the lock, try work, capture the generation, explicitly unlock,
        // then wait
        for (;;) {
            if (shutdown_.load(std::memory_order_acquire)) return false;

            std::unique_lock<std::mutex> lk(data_mutex_);
            if (count_ < Capacity) {
                // Insert item
                buffer_[tail_] = std::move(value);
                tail_ = (tail_ + 1) % Capacity;
                ++count_;
                // Publish availability to a consumer. Release ordering pairs
                // with the consumer's acquire wait.
                lk.unlock();
                not_empty_gen_.fetch_add(1, std::memory_order_release);
                not_empty_gen_.notify_one();
                return true;
            }
            // Queue is full: record current generation then sleep until it changes.
            uint64_t observed_gen = not_full_gen_.load(std::memory_order_relaxed);
            lk.unlock(); // unlock *before* sleeping to allow consumers to make space
            not_full_gen_.wait(observed_gen, std::memory_order_acquire);
            // Loop re-checks conditions.
        }
    }

    bool deq(T& value) {
        for (;;) {
            std::unique_lock<std::mutex> lk(data_mutex_);
            if (count_ > 0) {
                value = std::move(buffer_[head_]);
                head_ = (head_ + 1) % Capacity;
                --count_;
                lk.unlock();
                // Signal space available to a producer
                not_full_gen_.fetch_add(1, std::memory_order_release);
                not_full_gen_.notify_one();
                return true;
            }
            if (shutdown_.load(std::memory_order_acquire)) {
                // Shutdown + empty => no more work
                return false;
            }
            uint64_t observed_gen = not_empty_gen_.load(std::memory_order_relaxed);
            lk.unlock(); // release lock before blocking
            not_empty_gen_.wait(observed_gen, std::memory_order_acquire);
            // Loop: re-check state after wake (could be spurious or real).
        }
    }

    void shutdown() {
        shutdown_.store(true, std::memory_order_release);
        // Bump generations so any sleepers wake and see shutdown
        not_full_gen_.fetch_add(1, std::memory_order_release);
        not_empty_gen_.fetch_add(1, std::memory_order_release);
        not_full_gen_.notify_all();
        not_empty_gen_.notify_all();
    }

    size_t size() const {
        std::lock_guard<std::mutex> lk(data_mutex_);
        return count_;
    }
};

// Benchmark infrastructure
struct BenchmarkConfig {
    size_t num_producers;
    size_t num_consumers;
    size_t items_per_producer;
    size_t burst_size;
};

struct BenchmarkResults {
    double duration_ms;
    size_t total_items;
    double throughput_items_per_sec;
    size_t final_queue_size;
};

template<size_t Capacity>
BenchmarkResults run_benchmark(const BenchmarkConfig& config) {
    MPMCQueueAtomicWait<int, Capacity> queue;
    std::atomic<size_t> items_consumed{0};
    std::atomic<bool> producers_done{false};
    
    auto start = std::chrono::high_resolution_clock::now();
    
    // Launch producer threads
    std::vector<std::thread> producers;
    for (size_t p = 0; p < config.num_producers; ++p) {
        producers.emplace_back([&, p]() {
            std::mt19937 rng(p);
            std::uniform_int_distribution<int> dist(1, 1000);
            for (size_t i = 0; i < config.items_per_producer; i += config.burst_size) {
                // Produce in bursts
                for (size_t b = 0; b < config.burst_size && i + b < config.items_per_producer; ++b) {
                    queue.enq(dist(rng));
                }
                // Small delay to simulate work
                std::this_thread::sleep_for(std::chrono::microseconds(10));
            }
        });
    }
    
    // Launch consumer threads
    std::vector<std::thread> consumers;
    for (size_t c = 0; c < config.num_consumers; ++c) {
        consumers.emplace_back([&, c]() {
            int value;
            while (!producers_done.load(std::memory_order_acquire) || queue.size() > 0) {
                if (queue.deq(value)) {
                    items_consumed.fetch_add(1, std::memory_order_relaxed);
                    // Simulate processing
                    std::this_thread::sleep_for(std::chrono::microseconds(5));
                }
            }
        });
    }
    
    // Wait for producers to finish
    for (auto& t : producers) {
        t.join();
    }
    producers_done.store(true, std::memory_order_release);
    
    // Wait for consumers to finish
    // IMPORTANT: Wake any consumers waiting on empty queue so they can exit
    queue.shutdown();
    for (auto& t : consumers) {
        t.join();
    }
    
    auto end = std::chrono::high_resolution_clock::now();
    
    BenchmarkResults results;
    results.duration_ms = std::chrono::duration<double, std::milli>(end - start).count();
    results.total_items = items_consumed.load();
    results.throughput_items_per_sec = (results.total_items / results.duration_ms) * 1000.0;
    results.final_queue_size = queue.size();
    
    return results;
}

void print_results(const std::string& name, const BenchmarkConfig& config, const BenchmarkResults& results) {
    std::cout << "\n=== " << name << " ===" << std::endl;
    std::cout << "Config: " << config.num_producers << " producers, " 
              << config.num_consumers << " consumers, "
              << "burst=" << config.burst_size << std::endl;
    std::cout << "Duration: " << std::fixed << std::setprecision(2) 
              << results.duration_ms << " ms" << std::endl;
    std::cout << "Total items: " << results.total_items << std::endl;
    std::cout << "Throughput: " << std::fixed << std::setprecision(0) 
              << results.throughput_items_per_sec << " items/sec" << std::endl;
    std::cout << "Final queue size: " << results.final_queue_size << std::endl;
}

int main() {
    // Header suppressed for clean batch output
    
    std::vector<BenchmarkConfig> configs = {
        // Low contention: few threads
        {2, 2, 10000, 10},
        
        // High contention: many threads
        {8, 8, 10000, 1},
        
        // Bursty workload
        {4, 4, 10000, 50},
        
        // Asymmetric: more producers
        {8, 2, 5000, 5},
        
        // Asymmetric: more consumers
        {2, 8, 20000, 10},
    };
    
    constexpr size_t small_capacity = 10;
    constexpr size_t medium_capacity = 100;
    constexpr size_t large_capacity = 1000;
    
    std::vector<BenchmarkResults> all_small; all_small.reserve(2);
    std::vector<BenchmarkResults> all_medium; all_medium.reserve(2);
    std::vector<BenchmarkResults> all_large; all_large.reserve(1);
    for (size_t i = 0; i < 2; ++i) all_small.push_back(run_benchmark<small_capacity>(configs[i]));
    for (size_t i = 2; i < 4; ++i) all_medium.push_back(run_benchmark<medium_capacity>(configs[i]));
    for (size_t i = 4; i < configs.size(); ++i) all_large.push_back(run_benchmark<large_capacity>(configs[i]));

    // Consolidated table
    std::cout << "\n------------------------------------------------------------------------------------------\n";
    std::cout << "FINAL SUMMARY (atomic_wait)" << std::endl;
    std::cout << "------------------------------------------------------------------------------------------\n";
    std::cout << std::left << std::setw(6) << "Test"
              << std::right << std::setw(4) << "P"
              << std::setw(4) << "C"
              << std::setw(10) << "Capacity"
              << std::setw(7) << "Burst"
              << std::setw(14) << "Duration(ms)"
              << std::setw(16) << "Throughput/s"
              << std::setw(10) << "FinalQ" << std::endl;
    std::cout << "------------------------------------------------------------------------------------------\n";
    auto emit_row = [](size_t test_index, const BenchmarkConfig& cfg, const BenchmarkResults& r) {
        std::cout << std::left << std::setw(6) << test_index
                  << std::right << std::setw(4) << cfg.num_producers
                  << std::setw(4) << cfg.num_consumers
                  << std::setw(10) << (test_index <= 2 ? 10 : (test_index <= 4 ? 100 : 1000))
                  << std::setw(7) << cfg.burst_size
                  << std::setw(14) << std::fixed << std::setprecision(2) << r.duration_ms
                  << std::setw(16) << std::fixed << std::setprecision(0) << r.throughput_items_per_sec
                  << std::setw(10) << r.final_queue_size
                  << std::endl;
    };
    double agg_thr = 0.0;
    size_t test_no = 1;
    for (size_t i = 0; i < all_small.size(); ++i, ++test_no) { agg_thr += all_small[i].throughput_items_per_sec; emit_row(test_no, configs[i], all_small[i]); }
    for (size_t i = 0; i < all_medium.size(); ++i, ++test_no) { agg_thr += all_medium[i].throughput_items_per_sec; emit_row(test_no, configs[2 + i], all_medium[i]); }
    for (size_t i = 0; i < all_large.size(); ++i, ++test_no) { agg_thr += all_large[i].throughput_items_per_sec; emit_row(test_no, configs[4 + i], all_large[i]); }
    std::cout << "------------------------------------------------------------------------------------------\n";
    std::cout << "Aggregate throughput (items/sec): " << std::fixed << std::setprecision(0) << agg_thr << std::endl;
    std::cout << "------------------------------------------------------------------------------------------\n";
    
    return 0;
}
