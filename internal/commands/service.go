package commands

import (
	"context"
	"fmt"
	"strings"

	"github.com/robinlant/sklep-ds-bot/internal/domain"
)

type Repository interface {
	GetGuildSettings(context.Context, string) (*domain.GuildSettings, error)
	UpsertGuildSettings(context.Context, *domain.GuildSettings) error
	ListActiveSessionsByGuild(context.Context, string) ([]domain.Session, error)
	FindActiveSession(context.Context, string, string) (*domain.Session, error)
	ListActiveParticipantsByGuildSession(context.Context, string, string) ([]domain.ParticipantInterval, error)
	ListActiveParticipants(context.Context, string) ([]domain.ParticipantInterval, error)
}

type Service struct {
	repo Repository
}

func New(repo Repository) *Service {
	return &Service{repo: repo}
}

func (s *Service) GetGuildSettings(ctx context.Context, guildID string) (domain.GuildSettings, error) {
	if s.repo == nil {
		return domain.NewGuildSettings(guildID, domain.GuildTrackingModeAll, nil, ""), nil
	}
	settings, err := s.repo.GetGuildSettings(ctx, guildID)
	if err != nil {
		return domain.GuildSettings{}, err
	}
	if settings == nil {
		return domain.NewGuildSettings(guildID, domain.GuildTrackingModeAll, nil, ""), nil
	}
	return *settings, nil
}

func (s *Service) SetTrackingMode(ctx context.Context, guildID, mode string) (domain.GuildSettings, error) {
	settings, err := s.GetGuildSettings(ctx, guildID)
	if err != nil {
		return domain.GuildSettings{}, err
	}
	mode = domain.NormalizeTrackingMode(mode)
	settings.TrackingMode = mode
	return settings, s.save(ctx, settings)
}

func (s *Service) SetTrackedChannelIDs(ctx context.Context, guildID string, ids []string) (domain.GuildSettings, error) {
	settings, err := s.GetGuildSettings(ctx, guildID)
	if err != nil {
		return domain.GuildSettings{}, err
	}
	settings.TrackedChannelIDs = domain.CleanChannelIDs(ids)
	if len(settings.TrackedChannelIDs) == 0 {
		settings.TrackingMode = domain.GuildTrackingModeNone
	} else {
		settings.TrackingMode = domain.GuildTrackingModeSpecific
	}
	return settings, s.save(ctx, settings)
}

func (s *Service) AddTrackedChannel(ctx context.Context, guildID, channelID string) (domain.GuildSettings, error) {
	settings, err := s.GetGuildSettings(ctx, guildID)
	if err != nil {
		return domain.GuildSettings{}, err
	}
	currentMode := domain.NormalizeTrackingMode(settings.TrackingMode)
	channelID = strings.TrimSpace(channelID)
	if channelID == "" {
		return domain.GuildSettings{}, fmt.Errorf("channel id is required")
	}
	settings.TrackedChannelIDs = domain.CleanChannelIDs(append(settings.TrackedChannelIDs, channelID))
	if currentMode == domain.GuildTrackingModeAll {
		settings.TrackingMode = domain.GuildTrackingModeAll
	} else if len(settings.TrackedChannelIDs) == 0 {
		settings.TrackingMode = domain.GuildTrackingModeNone
	} else {
		settings.TrackingMode = domain.GuildTrackingModeSpecific
	}
	return settings, s.save(ctx, settings)
}

func (s *Service) RemoveTrackedChannel(ctx context.Context, guildID, channelID string) (domain.GuildSettings, error) {
	settings, err := s.GetGuildSettings(ctx, guildID)
	if err != nil {
		return domain.GuildSettings{}, err
	}
	channelID = strings.TrimSpace(channelID)
	if channelID == "" {
		return domain.GuildSettings{}, fmt.Errorf("channel id is required")
	}
	settings.TrackedChannelIDs = removeChannelID(settings.TrackedChannelIDs, channelID)
	if domain.NormalizeTrackingMode(settings.TrackingMode) != domain.GuildTrackingModeAll {
		if len(settings.TrackedChannelIDs) == 0 {
			settings.TrackingMode = domain.GuildTrackingModeNone
		} else {
			settings.TrackingMode = domain.GuildTrackingModeSpecific
		}
	}
	return settings, s.save(ctx, settings)
}

func (s *Service) ClearTrackedChannels(ctx context.Context, guildID string) (domain.GuildSettings, error) {
	settings, err := s.GetGuildSettings(ctx, guildID)
	if err != nil {
		return domain.GuildSettings{}, err
	}
	currentMode := domain.NormalizeTrackingMode(settings.TrackingMode)
	settings.TrackedChannelIDs = nil
	if currentMode != domain.GuildTrackingModeAll {
		settings.TrackingMode = domain.GuildTrackingModeNone
	}
	return settings, s.save(ctx, settings)
}

func (s *Service) ListTrackedChannels(ctx context.Context, guildID string) (domain.GuildSettings, error) {
	return s.GetGuildSettings(ctx, guildID)
}

func (s *Service) SetSummaryChannel(ctx context.Context, guildID, channelID string) (domain.GuildSettings, error) {
	settings, err := s.GetGuildSettings(ctx, guildID)
	if err != nil {
		return domain.GuildSettings{}, err
	}
	settings.SummaryChannelID = strings.TrimSpace(channelID)
	return settings, s.save(ctx, settings)
}

func (s *Service) ClearSummaryChannel(ctx context.Context, guildID string) (domain.GuildSettings, error) {
	return s.SetSummaryChannel(ctx, guildID, "")
}

func (s *Service) DescribeSettings(settings domain.GuildSettings) string {
	mode := domain.NormalizeTrackingMode(settings.TrackingMode)
	tracked := "all voice channels"
	stored := channelMentions(domain.CleanChannelIDs(settings.TrackedChannelIDs))
	if mode == domain.GuildTrackingModeNone {
		tracked = "no voice channels"
	} else if mode == domain.GuildTrackingModeSpecific {
		tracked = stored
		if tracked == "" {
			tracked = "no configured channels"
		}
	}

	summaryChannel := settings.SummaryChannelID
	if summaryChannel == "" {
		summaryChannel = "not set"
	} else {
		summaryChannel = channelMention(summaryChannel)
	}

	var lines []string
	lines = append(lines, fmt.Sprintf("tracking mode: %s", mode))
	lines = append(lines, fmt.Sprintf("tracked channels: %s", tracked))
	if stored != "" && stored != tracked {
		lines = append(lines, fmt.Sprintf("stored channels: %s", stored))
	}
	lines = append(lines, fmt.Sprintf("summary channel: %s", summaryChannel))
	return strings.Join(lines, "\n")
}

func (s *Service) save(ctx context.Context, settings domain.GuildSettings) error {
	if s.repo == nil {
		return nil
	}
	settings.GuildID = strings.TrimSpace(settings.GuildID)
	settings.TrackingMode = domain.NormalizeTrackingMode(settings.TrackingMode)
	settings.TrackedChannelIDs = domain.CleanChannelIDs(settings.TrackedChannelIDs)
	return s.repo.UpsertGuildSettings(ctx, &settings)
}

func removeChannelID(ids []string, target string) []string {
	out := make([]string, 0, len(ids))
	for _, id := range ids {
		if strings.TrimSpace(id) != target {
			out = append(out, id)
		}
	}
	return out
}

func channelMentions(ids []string) string {
	if len(ids) == 0 {
		return ""
	}
	parts := make([]string, 0, len(ids))
	for _, id := range ids {
		parts = append(parts, channelMention(id))
	}
	return strings.Join(parts, ", ")
}

func channelMention(id string) string {
	if id == "" {
		return ""
	}
	return "<#" + id + ">"
}
