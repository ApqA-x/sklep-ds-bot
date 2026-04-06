package summary

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/robinlant/sklep-ds-bot/internal/domain"
)

func TestBuildAndFormatSummary(t *testing.T) {
	startedAt := time.Date(2026, 4, 5, 18, 0, 0, 0, time.UTC)
	endedAt := startedAt.Add(1 * time.Hour)
	session := domain.Session{ID: "s1", GuildID: "g1", ChannelID: "c1", StartedAt: startedAt, EndedAt: &endedAt}

	participants := []domain.ParticipantInterval{
		{UserID: "u1", UserName: "alice", JoinedAt: startedAt, LeftAt: ptrTime(startedAt.Add(20 * time.Minute)), DurationMs: int64(20 * time.Minute / time.Millisecond)},
		{UserID: "u1", UserName: "alice", JoinedAt: startedAt.Add(30 * time.Minute), LeftAt: ptrTime(startedAt.Add(45 * time.Minute)), DurationMs: 0},
		{UserID: "u2", UserName: "bob", JoinedAt: startedAt.Add(10 * time.Minute), LeftAt: ptrTime(startedAt.Add(10 * time.Minute).Add(500 * time.Millisecond)), DurationMs: -1},
	}

	summary := BuildSummary(session, participants, "u2")
	if summary.UniqueUsers != 2 {
		t.Fatalf("UniqueUsers = %d, want 2", summary.UniqueUsers)
	}
	if len(summary.Participants) != 2 {
		t.Fatalf("Participants = %d, want 2", len(summary.Participants))
	}
	if summary.Participants[0].UserID != "u1" {
		t.Fatalf("expected u1 first, got %s", summary.Participants[0].UserID)
	}
	if summary.Participants[1].UserID != "u2" {
		t.Fatalf("expected u2 second, got %s", summary.Participants[1].UserID)
	}
	if summary.Participants[0].TotalTime <= summary.Participants[1].TotalTime {
		t.Fatalf("expected u1 to have more time than u2")
	}

	message := FormatSummary(summary)
	if message == "" {
		t.Fatal("expected summary message")
	}
	if strings.Contains(message, "<@") {
		t.Fatalf("expected no mention syntax, got: %s", message)
	}
	if !strings.Contains(message, "Ended by:") || !strings.Contains(message, "bob") {
		t.Fatalf("expected username in summary, got: %s", message)
	}
	if got := summary.TotalDuration; got != time.Hour {
		t.Fatalf("TotalDuration = %s, want 1h", got)
	}
	if summary.EndedByUserID != "u2" {
		t.Fatalf("EndedByUserID = %q, want u2", summary.EndedByUserID)
	}
}

func ptrTime(t time.Time) *time.Time { return &t }

type summaryFakeRepo struct {
	session  *domain.Session
	settings *domain.GuildSettings
	parts    []domain.ParticipantInterval
}

func (f *summaryFakeRepo) GetSessionByID(_ context.Context, _ string) (*domain.Session, error) {
	return f.session, nil
}
func (f *summaryFakeRepo) ListParticipantsBySession(_ context.Context, _ string) ([]domain.ParticipantInterval, error) {
	return f.parts, nil
}
func (f *summaryFakeRepo) GetGuildSettings(_ context.Context, _ string) (*domain.GuildSettings, error) {
	return f.settings, nil
}
func (f *summaryFakeRepo) ListClosedSessionsPendingSummary(_ context.Context) ([]domain.Session, error) {
	if f.session != nil && f.session.Status == domain.SessionStatusClosed && f.session.SummaryGeneratedAt == nil {
		return []domain.Session{*f.session}, nil
	}
	return nil, nil
}
func (f *summaryFakeRepo) MarkSessionSummaryReady(_ context.Context, _ string, _ string, _ string, _ time.Time) error {
	return nil
}

type summaryFakePublisher struct{ events []any }

func (f *summaryFakePublisher) PublishJSON(_ context.Context, _ string, value any) error {
	f.events = append(f.events, value)
	return nil
}

func TestHandleSessionClosedRequiresSummaryChannel(t *testing.T) {
	startedAt := time.Date(2026, 4, 5, 18, 0, 0, 0, time.UTC)
	endedAt := startedAt.Add(time.Hour)
	repo := &summaryFakeRepo{
		session: &domain.Session{ID: "s1", GuildID: "g1", ChannelID: "c1", StartedAt: startedAt, EndedAt: &endedAt},
		parts:   []domain.ParticipantInterval{{UserID: "u1", UserName: "alice", JoinedAt: startedAt, LeftAt: &endedAt, DurationMs: int64(time.Hour / time.Millisecond)}},
	}
	service := New(repo, &summaryFakePublisher{})
	if err := service.HandleSessionClosed(context.Background(), mustJSON(domain.SessionClosedEvent{SessionID: "s1", GuildID: "g1", ChannelID: "c1"})); err == nil {
		t.Fatal("expected missing summary channel error")
	}
}

func TestHandleSessionClosedUsesConfiguredSummaryChannel(t *testing.T) {
	startedAt := time.Date(2026, 4, 5, 18, 0, 0, 0, time.UTC)
	endedAt := startedAt.Add(time.Hour)
	repo := &summaryFakeRepo{
		session:  &domain.Session{ID: "s1", GuildID: "g1", ChannelID: "c1", StartedAt: startedAt, EndedAt: &endedAt},
		settings: &domain.GuildSettings{GuildID: "g1", SummaryChannelID: "text-1"},
		parts:    []domain.ParticipantInterval{{UserID: "u1", UserName: "alice", JoinedAt: startedAt, LeftAt: &endedAt, DurationMs: int64(time.Hour / time.Millisecond)}},
	}
	publisher := &summaryFakePublisher{}
	service := New(repo, publisher)
	if err := service.HandleSessionClosed(context.Background(), mustJSON(domain.SessionClosedEvent{SessionID: "s1", GuildID: "g1", ChannelID: "c1"})); err != nil {
		t.Fatal(err)
	}
	if len(publisher.events) != 1 {
		t.Fatalf("expected one summary event, got %d", len(publisher.events))
	}
	event, ok := publisher.events[0].(domain.SummaryReadyEvent)
	if !ok {
		t.Fatalf("unexpected event type %T", publisher.events[0])
	}
	if event.ChannelID != "text-1" {
		t.Fatalf("ChannelID = %q, want text-1", event.ChannelID)
	}
}

func mustJSON(v any) []byte {
	data, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return data
}
