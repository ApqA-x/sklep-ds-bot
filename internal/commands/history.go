package commands

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/robinlant/sklep-ds-bot/internal/domain"
)

const maxClosedHistoryItems = 10

func (s *Service) DescribeClosedSessionHistory(ctx context.Context, guildID, channelID string, limit int) (string, error) {
	if strings.TrimSpace(guildID) == "" {
		return "", fmt.Errorf("guild id is required")
	}
	if strings.TrimSpace(channelID) == "" {
		return "", fmt.Errorf("channel id is required")
	}
	if limit < 1 || limit > maxClosedHistoryItems {
		return "", fmt.Errorf("limit must be between 1 and %d", maxClosedHistoryItems)
	}

	sessions, err := s.repo.ListClosedSessionsByGuildChannel(ctx, guildID, channelID, limit+1)
	if err != nil {
		return "", err
	}

	filtered := make([]domain.Session, 0, len(sessions))
	for _, session := range sessions {
		if session.GuildID != guildID || session.ChannelID != channelID || session.Status != domain.SessionStatusClosed {
			continue
		}
		filtered = append(filtered, session)
	}
	if len(filtered) == 0 {
		return fmt.Sprintf("No closed sessions for <#%s>.", channelID), nil
	}

	hasMore := len(filtered) > limit
	if hasMore {
		filtered = filtered[:limit]
	}

	var builder strings.Builder
	builder.WriteString(fmt.Sprintf("Recent closed sessions for <#%s>\n", channelID))
	for i, session := range filtered {
		participants, err := s.repo.ListParticipantsByGuildChannelSession(ctx, guildID, channelID, session.ID)
		if err != nil {
			return "", err
		}
		summary := domain.BuildSessionSummary(session, participants, session.EndedByUserID)
		builder.WriteString(fmt.Sprintf("%d. ended %s, duration %s, %d users\n", i+1, formatRelativeTime(timeOrZero(session.EndedAt)), formatDuration(summary.TotalDuration), summary.UniqueUsers))
	}
	if hasMore {
		builder.WriteString("More sessions available.\n")
	}
	builder.WriteString(fmt.Sprintf("Use /voice inspect recent-session channel:<#%s> pick:<number> for details.\n", channelID))
	return strings.TrimSpace(builder.String()), nil
}

func (s *Service) DescribeClosedSessionDetail(ctx context.Context, guildID, channelID string, pick int) (string, error) {
	if strings.TrimSpace(guildID) == "" {
		return "", fmt.Errorf("guild id is required")
	}
	if strings.TrimSpace(channelID) == "" {
		return "", fmt.Errorf("channel id is required")
	}
	if pick < 1 || pick > maxClosedHistoryItems {
		return "", fmt.Errorf("pick must be between 1 and %d", maxClosedHistoryItems)
	}

	sessions, err := s.repo.ListClosedSessionsByGuildChannel(ctx, guildID, channelID, pick)
	if err != nil {
		return "", err
	}

	filtered := make([]domain.Session, 0, len(sessions))
	for _, session := range sessions {
		if session.GuildID != guildID || session.ChannelID != channelID || session.Status != domain.SessionStatusClosed {
			continue
		}
		filtered = append(filtered, session)
	}
	if len(filtered) < pick {
		return fmt.Sprintf("No closed session #%d for <#%s>.", pick, channelID), nil
	}

	session := filtered[pick-1]
	participants, err := s.repo.ListParticipantsByGuildChannelSession(ctx, guildID, channelID, session.ID)
	if err != nil {
		return "", err
	}
	summary := domain.BuildSessionSummary(session, participants, session.EndedByUserID)

	var builder strings.Builder
	builder.WriteString(fmt.Sprintf("Closed session for <#%s> (#%d most recent)\n", channelID, pick))
	builder.WriteString(fmt.Sprintf("Session ID: %s\n", session.ID))
	builder.WriteString(fmt.Sprintf("Started: %s\n", formatTime(session.StartedAt)))
	builder.WriteString(fmt.Sprintf("Ended: %s\n", formatTime(timeOrZero(session.EndedAt))))
	builder.WriteString(fmt.Sprintf("Duration: %s\n", formatDuration(summary.TotalDuration)))
	builder.WriteString(fmt.Sprintf("Unique users: %d\n", summary.UniqueUsers))
	if session.EndedByUserID != "" {
		builder.WriteString(fmt.Sprintf("Ended by: %s\n", participantDisplayName(summary.Participants, session.EndedByUserID)))
	}
	builder.WriteString("\nParticipants\n")
	if len(summary.Participants) == 0 {
		builder.WriteString("- none\n")
	}
	for _, participant := range summary.Participants {
		name := participant.UserName
		if name == "" {
			name = participant.UserID
		}
		builder.WriteString(fmt.Sprintf("- %s - %s (%s)\n", name, formatDuration(participant.TotalTime), intervalLabel(participant.Intervals)))
	}
	return strings.TrimSpace(builder.String()), nil
}

func participantDisplayName(participants []domain.ParticipantSummary, userID string) string {
	userID = strings.TrimSpace(userID)
	if userID == "" {
		return "unknown"
	}
	for _, participant := range participants {
		if participant.UserID == userID && strings.TrimSpace(participant.UserName) != "" {
			return participant.UserName
		}
	}
	return userID
}

func formatRelativeTime(t time.Time) string {
	if t.IsZero() {
		return "unknown"
	}
	return fmt.Sprintf("<t:%d:R>", t.UTC().Unix())
}

func timeOrZero(value *time.Time) time.Time {
	if value == nil {
		return time.Time{}
	}
	return *value
}

func intervalLabel(count int) string {
	if count == 1 {
		return "1 interval"
	}
	return fmt.Sprintf("%d intervals", count)
}
