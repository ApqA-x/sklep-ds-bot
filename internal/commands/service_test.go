package commands

import (
	"context"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/robinlant/sklep-ds-bot/internal/domain"
)

type fakeRepo struct {
	settings     map[string]domain.GuildSettings
	sessions     map[string]domain.Session
	participants map[string][]domain.ParticipantInterval
}

func newFakeRepo() *fakeRepo {
	return &fakeRepo{
		settings:     map[string]domain.GuildSettings{},
		sessions:     map[string]domain.Session{},
		participants: map[string][]domain.ParticipantInterval{},
	}
}

func (f *fakeRepo) GetGuildSettings(_ context.Context, guildID string) (*domain.GuildSettings, error) {
	settings, ok := f.settings[guildID]
	if !ok {
		return nil, nil
	}
	copy := settings
	return &copy, nil
}

func (f *fakeRepo) UpsertGuildSettings(_ context.Context, settings *domain.GuildSettings) error {
	copy := *settings
	f.settings[settings.GuildID] = copy
	return nil
}

func (f *fakeRepo) ListActiveSessionsByGuild(_ context.Context, guildID string) ([]domain.Session, error) {
	var out []domain.Session
	for _, session := range f.sessions {
		if session.GuildID == guildID && session.Status == domain.SessionStatusActive {
			out = append(out, session)
		}
	}
	return out, nil
}

func (f *fakeRepo) FindActiveSession(_ context.Context, guildID, channelID string) (*domain.Session, error) {
	for _, session := range f.sessions {
		if session.GuildID == guildID && session.ChannelID == channelID && session.Status == domain.SessionStatusActive {
			copy := session
			return &copy, nil
		}
	}
	return nil, nil
}

func (f *fakeRepo) ListActiveParticipants(_ context.Context, sessionID string) ([]domain.ParticipantInterval, error) {
	participants := f.participants[sessionID]
	var out []domain.ParticipantInterval
	for _, participant := range participants {
		if participant.Active {
			out = append(out, participant)
		}
	}
	return out, nil
}

func (f *fakeRepo) ListActiveParticipantsByGuildSession(_ context.Context, guildID, sessionID string) ([]domain.ParticipantInterval, error) {
	participants := f.participants[sessionID]
	var out []domain.ParticipantInterval
	for _, participant := range participants {
		if participant.Active && participant.GuildID == guildID {
			out = append(out, participant)
		}
	}
	return out, nil
}

func (f *fakeRepo) ListClosedSessionsByGuildChannel(_ context.Context, guildID, channelID string, limit int) ([]domain.Session, error) {
	var out []domain.Session
	for _, session := range f.sessions {
		if session.GuildID == guildID && session.ChannelID == channelID && session.Status == domain.SessionStatusClosed {
			out = append(out, session)
		}
	}
	sort.Slice(out, func(i, j int) bool {
		left := closedSessionEndTime(out[i])
		right := closedSessionEndTime(out[j])
		if left.Equal(right) {
			return out[i].StartedAt.After(out[j].StartedAt)
		}
		return left.After(right)
	})
	if len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

func (f *fakeRepo) ListParticipantsByGuildChannelSession(_ context.Context, guildID, channelID, sessionID string) ([]domain.ParticipantInterval, error) {
	participants := f.participants[sessionID]
	var out []domain.ParticipantInterval
	for _, participant := range participants {
		if participant.GuildID == guildID && participant.ChannelID == channelID {
			out = append(out, participant)
		}
	}
	return out, nil
}

func closedSessionEndTime(session domain.Session) time.Time {
	if session.EndedAt == nil {
		return time.Time{}
	}
	return *session.EndedAt
}

func ptrTime(t time.Time) *time.Time { return &t }

func TestSetTrackingMode(t *testing.T) {
	repo := newFakeRepo()
	svc := New(repo)

	settings, err := svc.SetTrackingMode(context.Background(), "g1", domain.GuildTrackingModeNone)
	if err != nil {
		t.Fatal(err)
	}
	if settings.TrackingMode != domain.GuildTrackingModeNone {
		t.Fatalf("tracking mode = %q, want %q", settings.TrackingMode, domain.GuildTrackingModeNone)
	}
}

func TestSetTrackingModeSpecificAllowedWithoutChannels(t *testing.T) {
	repo := newFakeRepo()
	svc := New(repo)

	settings, err := svc.SetTrackingMode(context.Background(), "g1", domain.GuildTrackingModeSpecific)
	if err != nil {
		t.Fatal(err)
	}
	if settings.TrackingMode != domain.GuildTrackingModeSpecific {
		t.Fatalf("tracking mode = %q, want %q", settings.TrackingMode, domain.GuildTrackingModeSpecific)
	}
}

func TestSetTrackedChannelIDs(t *testing.T) {
	repo := newFakeRepo()
	svc := New(repo)

	settings, err := svc.SetTrackedChannelIDs(context.Background(), "g1", []string{"c2", "c1", "c2"})
	if err != nil {
		t.Fatal(err)
	}
	if settings.TrackingMode != domain.GuildTrackingModeSpecific {
		t.Fatalf("tracking mode = %q, want %q", settings.TrackingMode, domain.GuildTrackingModeSpecific)
	}
	if len(settings.TrackedChannelIDs) != 2 || settings.TrackedChannelIDs[0] != "c1" || settings.TrackedChannelIDs[1] != "c2" {
		t.Fatalf("tracked ids = %#v, want sorted unique ids", settings.TrackedChannelIDs)
	}
}

func TestAddTrackedChannel(t *testing.T) {
	repo := newFakeRepo()
	repo.settings["g1"] = domain.NewGuildSettings("g1", domain.GuildTrackingModeSpecific, []string{"c1"}, "")
	svc := New(repo)

	settings, err := svc.AddTrackedChannel(context.Background(), "g1", "c2")
	if err != nil {
		t.Fatal(err)
	}
	settings, err = svc.AddTrackedChannel(context.Background(), "g1", "c1")
	if err != nil {
		t.Fatal(err)
	}
	settings, err = svc.AddTrackedChannel(context.Background(), "g1", "c2")
	if err != nil {
		t.Fatal(err)
	}
	if settings.TrackingMode != domain.GuildTrackingModeSpecific {
		t.Fatalf("tracking mode = %q, want %q", settings.TrackingMode, domain.GuildTrackingModeSpecific)
	}
	if len(settings.TrackedChannelIDs) != 2 || settings.TrackedChannelIDs[0] != "c1" || settings.TrackedChannelIDs[1] != "c2" {
		t.Fatalf("tracked ids = %#v, want deduped sorted ids", settings.TrackedChannelIDs)
	}
}

func TestAddTrackedChannelPreservesAllMode(t *testing.T) {
	repo := newFakeRepo()
	repo.settings["g1"] = domain.NewGuildSettings("g1", domain.GuildTrackingModeAll, nil, "")
	svc := New(repo)

	settings, err := svc.AddTrackedChannel(context.Background(), "g1", "c1")
	if err != nil {
		t.Fatal(err)
	}
	if settings.TrackingMode != domain.GuildTrackingModeAll {
		t.Fatalf("tracking mode = %q, want %q", settings.TrackingMode, domain.GuildTrackingModeAll)
	}
	if len(settings.TrackedChannelIDs) != 1 || settings.TrackedChannelIDs[0] != "c1" {
		t.Fatalf("tracked ids = %#v, want stored channel list", settings.TrackedChannelIDs)
	}
}

func TestRemoveTrackedChannelFallsBackToNone(t *testing.T) {
	repo := newFakeRepo()
	repo.settings["g1"] = domain.NewGuildSettings("g1", domain.GuildTrackingModeSpecific, []string{"c1", "c2"}, "")
	svc := New(repo)

	settings, err := svc.RemoveTrackedChannel(context.Background(), "g1", "c1")
	if err != nil {
		t.Fatal(err)
	}
	if settings.TrackingMode != domain.GuildTrackingModeSpecific {
		t.Fatalf("tracking mode = %q, want %q", settings.TrackingMode, domain.GuildTrackingModeSpecific)
	}
	if len(settings.TrackedChannelIDs) != 1 || settings.TrackedChannelIDs[0] != "c2" {
		t.Fatalf("tracked ids = %#v, want one remaining channel", settings.TrackedChannelIDs)
	}
	settings, err = svc.RemoveTrackedChannel(context.Background(), "g1", "c2")
	if err != nil {
		t.Fatal(err)
	}
	if settings.TrackingMode != domain.GuildTrackingModeNone {
		t.Fatalf("tracking mode = %q, want %q", settings.TrackingMode, domain.GuildTrackingModeNone)
	}
	if len(settings.TrackedChannelIDs) != 0 {
		t.Fatalf("tracked ids = %#v, want empty", settings.TrackedChannelIDs)
	}
}

func TestSetSummaryChannel(t *testing.T) {
	repo := newFakeRepo()
	svc := New(repo)

	settings, err := svc.SetSummaryChannel(context.Background(), "g1", "summary-1")
	if err != nil {
		t.Fatal(err)
	}
	if settings.SummaryChannelID != "summary-1" {
		t.Fatalf("summary channel = %q, want %q", settings.SummaryChannelID, "summary-1")
	}
}

func TestClearTrackedChannels(t *testing.T) {
	repo := newFakeRepo()
	repo.settings["g1"] = domain.NewGuildSettings("g1", domain.GuildTrackingModeSpecific, []string{"c1", "c2"}, "")
	svc := New(repo)

	settings, err := svc.ClearTrackedChannels(context.Background(), "g1")
	if err != nil {
		t.Fatal(err)
	}
	if settings.TrackingMode != domain.GuildTrackingModeNone || len(settings.TrackedChannelIDs) != 0 {
		t.Fatalf("unexpected cleared settings: %#v", settings)
	}
}

func TestClearTrackedChannelsPreservesAllMode(t *testing.T) {
	repo := newFakeRepo()
	repo.settings["g1"] = domain.NewGuildSettings("g1", domain.GuildTrackingModeAll, []string{"c1", "c2"}, "")
	svc := New(repo)

	settings, err := svc.ClearTrackedChannels(context.Background(), "g1")
	if err != nil {
		t.Fatal(err)
	}
	if settings.TrackingMode != domain.GuildTrackingModeAll || len(settings.TrackedChannelIDs) != 0 {
		t.Fatalf("unexpected cleared settings: %#v", settings)
	}
}

func TestDescribeSettingsShowsStoredChannelsInAllMode(t *testing.T) {
	svc := New(newFakeRepo())
	content := svc.DescribeSettings(domain.NewGuildSettings("g1", domain.GuildTrackingModeAll, []string{"c1", "c2"}, "summary-1"))
	if !strings.Contains(content, "stored channels: <#c1>, <#c2>") {
		t.Fatalf("unexpected content: %s", content)
	}
}

func TestDescribeActiveSessionsEmpty(t *testing.T) {
	repo := newFakeRepo()
	svc := New(repo)
	content, err := svc.DescribeActiveSessions(context.Background(), "g1")
	if err != nil {
		t.Fatal(err)
	}
	if content != "No active sessions." {
		t.Fatalf("unexpected content: %s", content)
	}
}

func TestDescribeActiveSessionsTruncates(t *testing.T) {
	repo := newFakeRepo()
	started := time.Date(2026, 4, 5, 18, 0, 0, 0, time.UTC)
	for i := 0; i < 11; i++ {
		id := string(rune('a' + i))
		repo.sessions[id] = domain.Session{ID: id, GuildID: "g1", ChannelID: "c" + id, Status: domain.SessionStatusActive, StartedAt: started.Add(time.Duration(i) * time.Minute)}
	}
	svc := New(repo)
	content, err := svc.DescribeActiveSessions(context.Background(), "g1")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(content, "+1 more sessions") {
		t.Fatalf("expected truncation, got: %s", content)
	}
}

func TestDescribeActiveSessionUsesGuildScopedParticipants(t *testing.T) {
	repo := newFakeRepo()
	started := time.Date(2026, 4, 5, 18, 0, 0, 0, time.UTC)
	repo.sessions["s1"] = domain.Session{ID: "s1", GuildID: "g1", ChannelID: "c1", Status: domain.SessionStatusActive, StartedAt: started}
	repo.participants["s1"] = []domain.ParticipantInterval{
		{ID: "p1", SessionID: "s1", GuildID: "g2", ChannelID: "c1", UserID: "u2", UserName: "bob", JoinedAt: started.Add(2 * time.Minute), Active: true},
	}
	svc := New(repo)
	content, err := svc.DescribeActiveSession(context.Background(), "g1", "c1")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(content, "no active participants") {
		t.Fatalf("expected guild-scoped empty participants, got: %s", content)
	}
}

func TestListActiveSessionsIsGuildScoped(t *testing.T) {
	repo := newFakeRepo()
	repo.sessions["s1"] = domain.Session{ID: "s1", GuildID: "g1", ChannelID: "c1", Status: domain.SessionStatusActive, StartedAt: time.Date(2026, 4, 5, 18, 0, 0, 0, time.UTC)}
	repo.sessions["s2"] = domain.Session{ID: "s2", GuildID: "g2", ChannelID: "c2", Status: domain.SessionStatusActive, StartedAt: time.Date(2026, 4, 5, 19, 0, 0, 0, time.UTC)}
	svc := New(repo)

	views, err := svc.ListActiveSessions(context.Background(), "g1")
	if err != nil {
		t.Fatal(err)
	}
	if len(views) != 1 || views[0].Session.GuildID != "g1" {
		t.Fatalf("views = %#v, want one guild-scoped session", views)
	}
}

func TestDescribeActiveSession(t *testing.T) {
	repo := newFakeRepo()
	started := time.Date(2026, 4, 5, 18, 0, 0, 0, time.UTC)
	repo.sessions["s1"] = domain.Session{ID: "s1", GuildID: "g1", ChannelID: "c1", Status: domain.SessionStatusActive, StartedAt: started}
	repo.participants["s1"] = []domain.ParticipantInterval{
		{ID: "p1", SessionID: "s1", GuildID: "g1", ChannelID: "c1", UserID: "u1", UserName: "alice", JoinedAt: started.Add(5 * time.Minute), Active: true},
	}
	svc := New(repo)

	content, err := svc.DescribeActiveSession(context.Background(), "g1", "c1")
	if err != nil {
		t.Fatal(err)
	}
	if content == "" || !contains(content, "alice") || !contains(content, "Channel: <#c1>") {
		t.Fatalf("unexpected content: %s", content)
	}
}

func TestDescribeClosedSessionHistory(t *testing.T) {
	repo := newFakeRepo()
	olderEnded := time.Date(2026, 4, 5, 18, 45, 0, 0, time.UTC)
	newerEnded := time.Date(2026, 4, 5, 19, 30, 0, 0, time.UTC)
	olderStarted := olderEnded.Add(-1 * time.Hour)
	newerStarted := newerEnded.Add(-30 * time.Minute)
	repo.sessions["old"] = domain.Session{ID: "old", GuildID: "g1", ChannelID: "c1", Status: domain.SessionStatusClosed, StartedAt: olderStarted, EndedAt: &olderEnded}
	repo.sessions["new"] = domain.Session{ID: "new", GuildID: "g1", ChannelID: "c1", Status: domain.SessionStatusClosed, StartedAt: newerStarted, EndedAt: &newerEnded}
	repo.participants["old"] = []domain.ParticipantInterval{
		{ID: "p1", SessionID: "old", GuildID: "g1", ChannelID: "c1", UserID: "u1", UserName: "alice", JoinedAt: olderStarted, LeftAt: ptrTime(olderEnded), DurationMs: int64(time.Hour / time.Millisecond)},
		{ID: "p2", SessionID: "old", GuildID: "g1", ChannelID: "c1", UserID: "u2", UserName: "bob", JoinedAt: olderStarted, LeftAt: ptrTime(olderEnded), DurationMs: int64(time.Hour / time.Millisecond)},
	}
	repo.participants["new"] = []domain.ParticipantInterval{
		{ID: "p3", SessionID: "new", GuildID: "g1", ChannelID: "c1", UserID: "u3", UserName: "carol", JoinedAt: newerStarted, LeftAt: ptrTime(newerEnded), DurationMs: int64(30 * time.Minute / time.Millisecond)},
	}
	svc := New(repo)

	content, err := svc.DescribeClosedSessionHistory(context.Background(), "g1", "c1", 5)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(content, "\n")
	if len(lines) < 3 {
		t.Fatalf("unexpected content: %s", content)
	}
	if !strings.Contains(lines[1], "1 users") || !strings.Contains(lines[2], "2 users") {
		t.Fatalf("expected newest closed session first, got: %s", content)
	}
	if !strings.Contains(content, "Recent closed sessions for <#c1>") {
		t.Fatalf("unexpected content: %s", content)
	}
}

func TestDescribeClosedSessionHistoryEmpty(t *testing.T) {
	svc := New(newFakeRepo())
	content, err := svc.DescribeClosedSessionHistory(context.Background(), "g1", "c1", 5)
	if err != nil {
		t.Fatal(err)
	}
	if content != "No closed sessions for <#c1>." {
		t.Fatalf("unexpected content: %s", content)
	}
}

func TestDescribeClosedSessionHistoryTruncates(t *testing.T) {
	repo := newFakeRepo()
	baseEnded := time.Date(2026, 4, 5, 20, 0, 0, 0, time.UTC)
	for i := 0; i < 6; i++ {
		ended := baseEnded.Add(-time.Duration(i) * time.Minute)
		started := ended.Add(-30 * time.Minute)
		id := string(rune('a' + i))
		repo.sessions[id] = domain.Session{ID: id, GuildID: "g1", ChannelID: "c1", Status: domain.SessionStatusClosed, StartedAt: started, EndedAt: &ended}
	}
	svc := New(repo)

	content, err := svc.DescribeClosedSessionHistory(context.Background(), "g1", "c1", 5)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(content, "More sessions available.") {
		t.Fatalf("expected truncation note, got: %s", content)
	}
}

func TestDescribeClosedSessionDetail(t *testing.T) {
	repo := newFakeRepo()
	newerEnded := time.Date(2026, 4, 5, 19, 30, 0, 0, time.UTC)
	olderEnded := time.Date(2026, 4, 5, 18, 45, 0, 0, time.UTC)
	newerStarted := newerEnded.Add(-30 * time.Minute)
	olderStarted := olderEnded.Add(-1 * time.Hour)
	repo.sessions["new"] = domain.Session{ID: "new", GuildID: "g1", ChannelID: "c1", Status: domain.SessionStatusClosed, StartedAt: newerStarted, EndedAt: &newerEnded}
	repo.sessions["old"] = domain.Session{ID: "old", GuildID: "g1", ChannelID: "c1", Status: domain.SessionStatusClosed, StartedAt: olderStarted, EndedAt: &olderEnded, EndedByUserID: "u2"}
	repo.participants["new"] = []domain.ParticipantInterval{
		{ID: "p1", SessionID: "new", GuildID: "g1", ChannelID: "c1", UserID: "u1", UserName: "alice", JoinedAt: newerStarted, LeftAt: ptrTime(newerEnded), DurationMs: int64(30 * time.Minute / time.Millisecond)},
	}
	repo.participants["old"] = []domain.ParticipantInterval{
		{ID: "p2", SessionID: "old", GuildID: "g1", ChannelID: "c1", UserID: "u1", UserName: "alice", JoinedAt: olderStarted, LeftAt: ptrTime(olderStarted.Add(20 * time.Minute)), DurationMs: int64(20 * time.Minute / time.Millisecond)},
		{ID: "p3", SessionID: "old", GuildID: "g1", ChannelID: "c1", UserID: "u1", UserName: "alice", JoinedAt: olderStarted.Add(25 * time.Minute), LeftAt: ptrTime(olderStarted.Add(35 * time.Minute)), DurationMs: int64(10 * time.Minute / time.Millisecond)},
		{ID: "p4", SessionID: "old", GuildID: "g1", ChannelID: "c1", UserID: "u2", UserName: "bob", JoinedAt: olderStarted, LeftAt: ptrTime(olderEnded), DurationMs: int64(time.Hour / time.Millisecond)},
	}
	svc := New(repo)

	content, err := svc.DescribeClosedSessionDetail(context.Background(), "g1", "c1", 2)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(content, "Closed session for <#c1> (#2 most recent)") {
		t.Fatalf("unexpected content: %s", content)
	}
	if !strings.Contains(content, "Session ID: old") || !strings.Contains(content, "Ended by: bob") {
		t.Fatalf("unexpected content: %s", content)
	}
	if !strings.Contains(content, "alice") || !strings.Contains(content, "2 intervals") {
		t.Fatalf("expected aggregated participant summary, got: %s", content)
	}
}

func TestDescribeClosedSessionDetailMissing(t *testing.T) {
	repo := newFakeRepo()
	ended := time.Date(2026, 4, 5, 19, 30, 0, 0, time.UTC)
	started := ended.Add(-30 * time.Minute)
	repo.sessions["new"] = domain.Session{ID: "new", GuildID: "g1", ChannelID: "c1", Status: domain.SessionStatusClosed, StartedAt: started, EndedAt: &ended}
	svc := New(repo)

	content, err := svc.DescribeClosedSessionDetail(context.Background(), "g1", "c1", 2)
	if err != nil {
		t.Fatal(err)
	}
	if content != "No closed session #2 for <#c1>." {
		t.Fatalf("unexpected content: %s", content)
	}
}

func contains(s, substr string) bool {
	return strings.Contains(s, substr)
}
