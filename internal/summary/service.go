package summary

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/robinlant/sklep-ds-bot/internal/domain"
)

type Repository interface {
	GetSessionByID(context.Context, string) (*domain.Session, error)
	ListParticipantsBySession(context.Context, string) ([]domain.ParticipantInterval, error)
	GetGuildSettings(context.Context, string) (*domain.GuildSettings, error)
	ListClosedSessionsPendingSummary(context.Context) ([]domain.Session, error)
	MarkSessionSummaryReady(context.Context, string, string, string, time.Time) error
}

type Publisher interface {
	PublishJSON(context.Context, string, any) error
}

type Service struct {
	repo      Repository
	publisher Publisher
}

func New(repo Repository, publisher Publisher) *Service {
	return &Service{repo: repo, publisher: publisher}
}

func (s *Service) HandleSessionClosed(ctx context.Context, payload []byte) error {
	return s.generateAndPublish(ctx, payload)
}

func (s *Service) Start(ctx context.Context) error {
	pending, err := s.repo.ListClosedSessionsPendingSummary(ctx)
	if err != nil {
		return err
	}
	for _, session := range pending {
		if err := s.generateAndPublishForSession(ctx, session); err != nil {
			continue
		}
	}
	return nil
}

func (s *Service) generateAndPublish(ctx context.Context, payload []byte) error {
	var event domain.SessionClosedEvent
	if err := json.Unmarshal(payload, &event); err != nil {
		return err
	}

	session, err := s.repo.GetSessionByID(ctx, event.SessionID)
	if err != nil {
		return err
	}
	if session == nil {
		return nil
	}
	if session.SummaryGeneratedAt != nil || session.SummaryDeliveredAt != nil {
		return nil
	}
	if session.GuildID != event.GuildID || session.ChannelID != event.ChannelID {
		return fmt.Errorf("session mismatch for summary event")
	}

	participants, err := s.repo.ListParticipantsBySession(ctx, event.SessionID)
	if err != nil {
		return err
	}

	summary := BuildSummary(*session, participants, event.EndedByUserID)
	settings, err := s.repo.GetGuildSettings(ctx, event.GuildID)
	if err != nil {
		return err
	}
	if settings == nil || settings.SummaryChannelID == "" {
		return fmt.Errorf("summary channel not configured")
	}
	destination := settings.SummaryDestination("")
	message := FormatSummary(summary)
	readyAt := time.Now().UTC()
	if err := s.repo.MarkSessionSummaryReady(ctx, session.ID, destination, message, readyAt); err != nil {
		return err
	}
	return s.publisher.PublishJSON(ctx, domain.SubjectSummaryReady, domain.SummaryReadyEvent{
		SessionID: event.SessionID,
		GuildID:   event.GuildID,
		ChannelID: destination,
		Message:   message,
	})
}

func (s *Service) generateAndPublishForSession(ctx context.Context, session domain.Session) error {
	participants, err := s.repo.ListParticipantsBySession(ctx, session.ID)
	if err != nil {
		return err
	}
	event := domain.SessionClosedEvent{
		SessionID:     session.ID,
		GuildID:       session.GuildID,
		ChannelID:     session.ChannelID,
		StartedAt:     session.StartedAt,
		EndedAt:       derefTime(session.EndedAt),
		EndedByUserID: session.EndedByUserID,
	}
	return s.generateAndPublishFromSession(ctx, session, event, participants)
}

func (s *Service) generateAndPublishFromSession(ctx context.Context, session domain.Session, event domain.SessionClosedEvent, participants []domain.ParticipantInterval) error {
	if session.GuildID != event.GuildID || session.ChannelID != event.ChannelID {
		return fmt.Errorf("session mismatch for summary event")
	}
	summary := BuildSummary(session, participants, event.EndedByUserID)
	settings, err := s.repo.GetGuildSettings(ctx, event.GuildID)
	if err != nil {
		return err
	}
	if settings == nil || settings.SummaryChannelID == "" {
		return fmt.Errorf("summary channel not configured")
	}
	destination := settings.SummaryDestination("")
	message := FormatSummary(summary)
	readyAt := time.Now().UTC()
	if err := s.repo.MarkSessionSummaryReady(ctx, session.ID, destination, message, readyAt); err != nil {
		return err
	}
	return s.publisher.PublishJSON(ctx, domain.SubjectSummaryReady, domain.SummaryReadyEvent{
		SessionID: session.ID,
		GuildID:   event.GuildID,
		ChannelID: destination,
		Message:   message,
	})
}

func BuildSummary(session domain.Session, participants []domain.ParticipantInterval, endedByUserID string) domain.SessionSummary {
	return domain.BuildSessionSummary(session, participants, endedByUserID)
}

func FormatSummary(summary domain.SessionSummary) string {
	var builder strings.Builder
	builder.WriteString("**Voice session ended**\n")
	builder.WriteString(fmt.Sprintf("**Channel:** <#%s>\n", summary.ChannelID))
	builder.WriteString(fmt.Sprintf("**Duration:** %s\n", summary.TotalDuration.Round(time.Second)))
	builder.WriteString(fmt.Sprintf("**Unique users:** %d\n", summary.UniqueUsers))
	if summary.EndedByUserID != "" {
		builder.WriteString(fmt.Sprintf("**Ended by:** %s\n", participantDisplayName(summary.Participants, summary.EndedByUserID)))
	}
	builder.WriteString("\n**Participants**\n")
	if len(summary.Participants) == 0 {
		builder.WriteString("- none\n")
	}
	for _, participant := range summary.Participants {
		name := participant.UserName
		if name == "" {
			name = participant.UserID
		}
		builder.WriteString(fmt.Sprintf("- %s - %s (%s)\n", name, participant.TotalTime.Round(time.Second), intervalLabel(participant.Intervals)))
	}
	return strings.TrimSpace(builder.String())
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

func intervalLabel(count int) string {
	if count == 1 {
		return "1 interval"
	}
	return fmt.Sprintf("%d intervals", count)
}

func derefTime(value *time.Time) time.Time {
	if value == nil {
		return time.Time{}
	}
	return *value
}
