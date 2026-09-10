// SPDX-License-Identifier: AGPL-3.0-or-later
package irc

import (
	"context"
	"sync"
	"time"
)

// TokenBucket implements a standard token bucket rate limiter.
type TokenBucket struct {
	mu         sync.Mutex
	capacity   float64
	refillRate float64 // tokens per second
	tokens     float64
	lastRefill time.Time
}

// NewTokenBucket initializes a rate limiter.
func NewTokenBucket(capacity, refillRate float64) *TokenBucket {
	return &TokenBucket{
		capacity:   capacity,
		refillRate: refillRate,
		tokens:     capacity,
		lastRefill: time.Now(),
	}
}

// Take blocks until a token is available.
func (tb *TokenBucket) Take() {
	tb.TakeContext(context.Background())
}

// TakeContext blocks until a token is available or the context is cancelled.
// Returns true if a token was acquired, false if cancelled.
func (tb *TokenBucket) TakeContext(ctx context.Context) bool {
	tb.mu.Lock()
	defer tb.mu.Unlock()

	for {
		select {
		case <-ctx.Done():
			return false
		default:
		}

		now := time.Now()
		elapsed := now.Sub(tb.lastRefill).Seconds()
		tb.tokens += elapsed * tb.refillRate
		if tb.tokens > tb.capacity {
			tb.tokens = tb.capacity
		}
		tb.lastRefill = now

		if tb.tokens >= 1.0 {
			tb.tokens -= 1.0
			return true
		}

		needed := 1.0 - tb.tokens
		waitTime := time.Duration((needed / tb.refillRate) * float64(time.Second))
		if waitTime <= 0 {
			waitTime = 10 * time.Millisecond
		}
		tb.mu.Unlock()

		select {
		case <-ctx.Done():
			tb.mu.Lock()
			return false
		case <-time.After(waitTime):
		}

		tb.mu.Lock()
	}
}
