/**
 * Bounded Multi-Producer Multi-Consumer Queue
 * Traditional Implementation: mutex + condition_variable
 * 
 * This implementation uses the classic synchronization primitives that have been
 * available in C++ since C++11. It relies on:
 * - std::mutex for mutual exclusion
 * - std::condition_variable for blocking wait/notify
 * 
 * Trade-offs:
 * + Well-understood, portable, and battle-tested
 * + Works on all C++11+ compilers
 * - Higher overhead due to kernel transitions on wait/notify
 * - Potential thundering herd problem when many threads wait
 * - More context switches under high contention
 * # Rebuild (MinGW g++ on Windows)
 * 
 * Build:
 * g++ -std=c++20 -O2 -pthread mpmc_mutex_cv.cpp -o mpmc_mutex_cv
 * 
 * Run:
 * \mpmc_mutex_cv.exe
 */

#include <iostream>
#include <mutex>
#include <condition_variable>
#include <thread>
#include <vector>
#include <chrono>
#include <atomic>
#include <random>
#include <iomanip>
#include <array>

template<typename T, size_t Capacity>
class MPMCQueueMutexCV {
private:
    // Circular buffer storage (fixed capacity)
    std::array<T, Capacity> buffer_{};
    size_t head_ = 0;
    size_t tail_ = 0;
    // Align hot counter to avoid false sharing
    alignas(64) size_t count_ = 0;
    mutable std::mutex mutex_;
    std::condition_variable cv_not_full_;
    std::condition_variable cv_not_empty_;
    std::atomic<bool> shutdown_{false};

public:
    MPMCQueueMutexCV() = default;

    // Producer: Enqueue item
    bool enq(T value) {
        std::unique_lock<std::mutex> lock(mutex_);

        // Wait while queue is full (using condition variable)
        cv_not_full_.wait(lock, [this] {
            return count_ < Capacity || shutdown_.load(std::memory_order_acquire);
        });

        if (shutdown_.load(std::memory_order_acquire)) return false;

        // Insert item into circular buffer
        buffer_[tail_] = std::move(value);
        tail_ = (tail_ + 1) % Capacity;
        ++count_;

        // Release the lock before notifying to avoid waking a thread
        // that will immediately block trying to reacquire the mutex
        lock.unlock();
        cv_not_empty_.notify_one();
        return true;
    }

    // Consumer: Dequeue item
    bool deq(T& value) {
        std::unique_lock<std::mutex> lock(mutex_);

        // Wait while queue is empty (using condition variable)
        cv_not_empty_.wait(lock, [this] {
            return count_ > 0 || shutdown_.load(std::memory_order_acquire);
        });

        if (shutdown_.load(std::memory_order_acquire) && count_ == 0) return false;

        // Remove item from circular buffer
        value = std::move(buffer_[head_]);
        head_ = (head_ + 1) % Capacity;
        --count_;

        // Release the lock before notifying to avoid waking a thread
        // that will immediately block trying to reacquire the mutex
        lock.unlock();
        cv_not_full_.notify_one();
        return true;
    }

    void shutdown() {
        // Set the atomic shutdown flag and wake all waiters
        shutdown_.store(true, std::memory_order_release);
        cv_not_full_.notify_all();
        cv_not_empty_.notify_all();
    }

    size_t size() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return count_;
    }
};

// Benchmark infrastructure
struct BenchmarkConfig {
    size_t num_producers;
    size_t num_consumers;
    size_t queue_capacity;
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
    MPMCQueueMutexCV<int, Capacity> queue;
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

    // IMPORTANT: Wake any consumers waiting on empty queue so they can drain and exit
    queue.shutdown();

    // Wait for consumers to finish
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
    // no-op: legacy single-test printer retained for compatibility if needed
}

int main() {
    std::vector<BenchmarkConfig> configs = {
        // Low contention: few threads
        {2, 2, 10, 10000, 10},
        
        // High contention: many threads
        {8, 8, 10, 10000, 1},
        
        // Bursty: moderate threads, large bursts
        {4, 4, 100, 10000, 50},
        
        // Asymmetric: more producers than consumers
        {8, 2, 100, 5000, 5},
        
        // Asymmetric: more consumers than producers
        {2, 8, 1000, 20000, 10},
    };
    
    constexpr size_t small_capacity = 10;
    constexpr size_t medium_capacity = 100;
    constexpr size_t large_capacity = 1000;
    std::vector<BenchmarkResults> all_results;
    all_results.reserve(configs.size());
    for (size_t i = 0; i < 2; ++i) all_results.push_back(run_benchmark<small_capacity>(configs[i]));
    for (size_t i = 2; i < 4; ++i) all_results.push_back(run_benchmark<medium_capacity>(configs[i]));
    for (size_t i = 4; i < configs.size(); ++i) all_results.push_back(run_benchmark<large_capacity>(configs[i]));

    // Final consolidated table
    std::cout << "\n------------------------------------------------------------------------------------------\n";
    std::cout << "FINAL SUMMARY (Mutex+CV)" << std::endl;
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
    double agg_thr = 0.0;
    for (size_t i = 0; i < configs.size(); ++i) {
        const auto& cfg = configs[i];
        const auto& r = all_results[i];
        agg_thr += r.throughput_items_per_sec;
        std::cout << std::left << std::setw(6) << (i + 1)
                  << std::right << std::setw(4) << cfg.num_producers
                  << std::setw(4) << cfg.num_consumers
                  << std::setw(10) << cfg.queue_capacity
                  << std::setw(7) << cfg.burst_size
                  << std::setw(14) << std::fixed << std::setprecision(2) << r.duration_ms
                  << std::setw(16) << std::fixed << std::setprecision(0) << r.throughput_items_per_sec
                  << std::setw(10) << r.final_queue_size
                  << std::endl;
    }
    std::cout << "------------------------------------------------------------------------------------------\n";
    std::cout << "Aggregate throughput (items/sec): " << std::fixed << std::setprecision(0) << agg_thr << std::endl;
    std::cout << "------------------------------------------------------------------------------------------\n";
    
    return 0;
}
