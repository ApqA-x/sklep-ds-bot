package commands

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/robinlant/sklep-ds-bot/internal/domain"
)

type SessionParticipantView struct {
	UserID    string
	UserName  string
	JoinedAt  time.Time
	ActiveFor time.Duration
}

type ActiveSessionView struct {
	Session      domain.Session
	Participants []SessionParticipantView
	ActiveFor    time.Duration
	Count        int
}

func (s *Service) ListActiveSessions(ctx context.Context, guildID string) ([]ActiveSessionView, error) {
	sessions, err := s.repo.ListActiveSessionsByGuild(ctx, guildID)
	if err != nil {
		return nil, err
	}
	views := make([]ActiveSessionView, 0, len(sessions))
	for _, session := range sessions {
		view, err := s.buildActiveSessionView(ctx, session.GuildID, session)
		if err != nil {
			return nil, err
		}
		views = append(views, view)
	}
	sort.Slice(views, func(i, j int) bool {
		if views[i].Session.StartedAt.Equal(views[j].Session.StartedAt) {
			return views[i].Session.ChannelID < views[j].Session.ChannelID
		}
		return views[i].Session.StartedAt.After(views[j].Session.StartedAt)
	})
	return views, nil
}

func (s *Service) InspectActiveSession(ctx context.Context, guildID, channelID string) (*ActiveSessionView, error) {
	session, err := s.repo.FindActiveSession(ctx, guildID, channelID)
	if err != nil || session == nil {
		return nil, err
	}
	view, err := s.buildActiveSessionView(ctx, guildID, *session)
	if err != nil {
		return nil, err
	}
	return &view, nil
}

func (s *Service) DescribeActiveSessions(ctx context.Context, guildID string) (string, error) {
	views, err := s.ListActiveSessions(ctx, guildID)
	if err != nil {
		return "", err
	}
	if len(views) == 0 {
		return "No active sessions.", nil
	}

	var builder strings.Builder
	builder.WriteString("Active sessions\n")
	limit := len(views)
	if limit > 10 {
		limit = 10
	}
	for i := 0; i < limit; i++ {
		view := views[i]
		builder.WriteString(fmt.Sprintf("- <#%s>: %d users, running %s\n", view.Session.ChannelID, view.Count, formatDuration(view.ActiveFor)))
	}
	if len(views) > limit {
		builder.WriteString(fmt.Sprintf("+%d more sessions\n", len(views)-limit))
	}
	return strings.TrimSpace(builder.String()), nil
}

func (s *Service) DescribeActiveSession(ctx context.Context, guildID, channelID string) (string, error) {
	view, err := s.InspectActiveSession(ctx, guildID, channelID)
	if err != nil {
		return "", err
	}
	if view == nil {
		return "No active session in that channel.", nil
	}

	var builder strings.Builder
	builder.WriteString(fmt.Sprintf("Channel: <#%s>\n", view.Session.ChannelID))
	builder.WriteString(fmt.Sprintf("Started: %s\n", formatTime(view.Session.StartedAt)))
	builder.WriteString(fmt.Sprintf("Running: %s\n", formatDuration(view.ActiveFor)))
	builder.WriteString(fmt.Sprintf("Participants: %d\n", view.Count))
	if len(view.Participants) == 0 {
		builder.WriteString("- no active participants\n")
	} else {
		limit := len(view.Participants)
		if limit > 10 {
			limit = 10
		}
		for i := 0; i < limit; i++ {
			participant := view.Participants[i]
			name := participant.UserName
			if name == "" {
				name = participant.UserID
			}
			builder.WriteString(fmt.Sprintf("- %s: %s (joined %s)\n", name, formatDuration(participant.ActiveFor), formatTime(participant.JoinedAt)))
		}
		if len(view.Participants) > limit {
			builder.WriteString(fmt.Sprintf("+%d more participants\n", len(view.Participants)-limit))
		}
	}
	return strings.TrimSpace(builder.String()), nil
}

func (s *Service) buildActiveSessionView(ctx context.Context, guildID string, session domain.Session) (ActiveSessionView, error) {
	participants, err := s.repo.ListActiveParticipantsByGuildSession(ctx, guildID, session.ID)
	if err != nil {
		return ActiveSessionView{}, err
	}
	now := time.Now().UTC()
	view := ActiveSessionView{
		Session:   session,
		ActiveFor: now.Sub(session.StartedAt),
		Count:     len(participants),
	}
	for _, participant := range participants {
		view.Participants = append(view.Participants, SessionParticipantView{
			UserID:    participant.UserID,
			UserName:  participant.UserName,
			JoinedAt:  participant.JoinedAt,
			ActiveFor: now.Sub(participant.JoinedAt),
		})
	}
	sort.Slice(view.Participants, func(i, j int) bool {
		if view.Participants[i].JoinedAt.Equal(view.Participants[j].JoinedAt) {
			return view.Participants[i].UserName < view.Participants[j].UserName
		}
		return view.Participants[i].JoinedAt.Before(view.Participants[j].JoinedAt)
	})
	return view, nil
}

func formatDuration(d time.Duration) string {
	if d < 0 {
		d = 0
	}
	return d.Round(time.Second).String()
}

func formatTime(t time.Time) string {
	if t.IsZero() {
		return "unknown"
	}
	unix := t.UTC().Unix()
	return fmt.Sprintf("<t:%d:F> (<t:%d:R>)", unix, unix)
}
