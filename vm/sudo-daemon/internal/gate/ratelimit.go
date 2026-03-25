// Package gate implements the core request handling logic for sudo-gated.
package gate

import (
	"time"

	"golang.org/x/time/rate"
)

// NewRateLimiter creates a token-bucket rate limiter.
//
// requestsPerMinute is the sustained rate (e.g., 5 means one token every 12s).
// burst is the maximum number of tokens that can be consumed at once.
func NewRateLimiter(requestsPerMinute float64, burst int) *rate.Limiter {
	interval := time.Duration(float64(time.Minute) / requestsPerMinute)
	return rate.NewLimiter(rate.Every(interval), burst)
}
