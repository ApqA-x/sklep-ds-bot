package main

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestWaitForBotUserID(t *testing.T) {
	ready := make(chan string, 1)
	ready <- "bot"
	userID, err := waitForBotUserID(context.Background(), ready, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if userID != "bot" {
		t.Fatalf("userID = %q, want bot", userID)
	}
}

func TestWaitForBotUserIDTimeout(t *testing.T) {
	_, err := waitForBotUserID(context.Background(), make(chan string), time.Millisecond)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("err = %v, want deadline exceeded", err)
	}
}

func TestWaitForBotUserIDCanceled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := waitForBotUserID(ctx, make(chan string), time.Second)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want canceled", err)
	}
}

func TestWaitForBotUserIDEmptyPayload(t *testing.T) {
	ready := make(chan string, 1)
	ready <- ""
	_, err := waitForBotUserID(context.Background(), ready, time.Second)
	if err == nil || err.Error() != "discord ready event missing bot user id" {
		t.Fatalf("err = %v, want empty payload error", err)
	}
}
