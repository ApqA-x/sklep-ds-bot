package commands

import (
	"context"
	"fmt"
	"math"
	"strings"
	"time"

	"github.com/robinlant/sklep-ds-bot/internal/botauth"
	"github.com/robinlant/sklep-ds-bot/internal/domain"

	"github.com/bwmarrin/discordgo"
)

const (
	voiceCommandName                     = "voice"
	voiceInspectHistoryCommandName       = "history"
	voiceInspectRecentSessionCommandName  = "recent-session"
)

func (s *Service) Install(session *discordgo.Session, allowedGuildID string, botAdminUserIDs []string) {
	session.AddHandler(func(ds *discordgo.Session, interaction *discordgo.InteractionCreate) {
		if interaction.Type != discordgo.InteractionApplicationCommand {
			return
		}
		if interaction.GuildID == "" {
			_ = respondEphemeral(ds, interaction, "This command can only be used in a server.")
			return
		}
		if strings.TrimSpace(allowedGuildID) == "" || interaction.GuildID != allowedGuildID {
			return
		}

		data := interaction.ApplicationCommandData()
		if data.Name != voiceCommandName {
			return
		}

		group, command, options := parseVoiceRoute(data.Options)
		if !canUseVoiceCommand(interaction, botAdminUserIDs, group, command) {
			_ = respondEphemeral(ds, interaction, "Insufficient permissions.")
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()

		content, err := s.handleVoiceCommand(ctx, interaction, group, command, options)
		if err != nil {
			content = err.Error()
		}
		if content == "" {
			content = "Done."
		}
		_ = respondEphemeral(ds, interaction, content)
	})
}

func VoiceApplicationCommand() *discordgo.ApplicationCommand {
	return &discordgo.ApplicationCommand{
		Name:        voiceCommandName,
		Description: "Manage tracked voice channels and inspect live and closed sessions",
		Options: []*discordgo.ApplicationCommandOption{
			{
				Type:        discordgo.ApplicationCommandOptionSubCommandGroup,
				Name:        "config",
				Description: "Configure voice tracking",
				Options: []*discordgo.ApplicationCommandOption{
					{
						Type:        discordgo.ApplicationCommandOptionSubCommand,
						Name:        "mode",
						Description: "Show or set the tracking mode",
						Options: []*discordgo.ApplicationCommandOption{
							{
								Type:        discordgo.ApplicationCommandOptionString,
								Name:        "action",
								Description: "show or set",
								Required:    true,
								Choices:     []*discordgo.ApplicationCommandOptionChoice{{Name: "show", Value: "show"}, {Name: "set", Value: "set"}},
							},
							{
								Type:        discordgo.ApplicationCommandOptionString,
								Name:        "value",
								Description: "all, none, or specific",
								Choices:     []*discordgo.ApplicationCommandOptionChoice{{Name: domain.GuildTrackingModeAll, Value: domain.GuildTrackingModeAll}, {Name: domain.GuildTrackingModeNone, Value: domain.GuildTrackingModeNone}, {Name: domain.GuildTrackingModeSpecific, Value: domain.GuildTrackingModeSpecific}},
							},
						},
					},
					{
						Type:        discordgo.ApplicationCommandOptionSubCommand,
						Name:        "channels",
						Description: "Manage tracked voice channels",
						Options: []*discordgo.ApplicationCommandOption{
							{
								Type:        discordgo.ApplicationCommandOptionString,
								Name:        "action",
								Description: "add, remove, list, or clear",
								Required:    true,
								Choices:     []*discordgo.ApplicationCommandOptionChoice{{Name: "add", Value: "add"}, {Name: "remove", Value: "remove"}, {Name: "list", Value: "list"}, {Name: "clear", Value: "clear"}},
							},
							{
								Type:         discordgo.ApplicationCommandOptionChannel,
								Name:         "channel",
								Description:  "Voice channel",
								ChannelTypes: []discordgo.ChannelType{discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice},
							},
						},
					},
					{
						Type:        discordgo.ApplicationCommandOptionSubCommand,
						Name:        "summary-channel",
						Description: "Set or clear the summary destination",
						Options: []*discordgo.ApplicationCommandOption{
							{
								Type:        discordgo.ApplicationCommandOptionString,
								Name:        "action",
								Description: "set or clear",
								Required:    true,
								Choices:     []*discordgo.ApplicationCommandOptionChoice{{Name: "set", Value: "set"}, {Name: "clear", Value: "clear"}},
							},
							{
								Type:         discordgo.ApplicationCommandOptionChannel,
								Name:         "channel",
								Description:  "Destination text channel",
								ChannelTypes: []discordgo.ChannelType{discordgo.ChannelTypeGuildText},
							},
						},
					},
				},
			},
			{
				Type:        discordgo.ApplicationCommandOptionSubCommandGroup,
				Name:        "inspect",
				Description: "Inspect live and closed voice sessions",
				Options: []*discordgo.ApplicationCommandOption{
					{
						Type:        discordgo.ApplicationCommandOptionSubCommand,
						Name:        "settings",
						Description: "Show current settings",
					},
					{
						Type:        discordgo.ApplicationCommandOptionSubCommand,
						Name:        "sessions",
						Description: "List active sessions (Admin only)",
					},
					{
						Type:        discordgo.ApplicationCommandOptionSubCommand,
						Name:        "session",
						Description: "Inspect one active session (Admin only)",
						Options: []*discordgo.ApplicationCommandOption{{
							Type:         discordgo.ApplicationCommandOptionChannel,
							Name:         "channel",
							Description:  "Tracked voice channel",
							Required:     true,
							ChannelTypes: []discordgo.ChannelType{discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice},
						}},
					},
					{
						Type:        discordgo.ApplicationCommandOptionSubCommand,
						Name:        voiceInspectHistoryCommandName,
						Description: "List recent closed sessions for one voice channel",
						Options: []*discordgo.ApplicationCommandOption{
							{
								Type:         discordgo.ApplicationCommandOptionChannel,
								Name:         "channel",
								Description:  "Voice channel to inspect",
								Required:     true,
								ChannelTypes: []discordgo.ChannelType{discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice},
							},
							{
								Type:        discordgo.ApplicationCommandOptionInteger,
								Name:        "limit",
								Description: "How many closed sessions to show (1-10)",
							},
						},
					},
					{
						Type:        discordgo.ApplicationCommandOptionSubCommand,
						Name:        voiceInspectRecentSessionCommandName,
						Description: "Inspect one recent closed session for a voice channel",
						Options: []*discordgo.ApplicationCommandOption{
							{
								Type:         discordgo.ApplicationCommandOptionChannel,
								Name:         "channel",
								Description:  "Voice channel to inspect",
								Required:     true,
								ChannelTypes: []discordgo.ChannelType{discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice},
							},
							{
								Type:        discordgo.ApplicationCommandOptionInteger,
								Name:        "pick",
								Description: "Which recent closed session to inspect (1-10)",
							},
						},
					},
				},
			},
		},
	}
}

func (s *Service) handleVoiceCommand(ctx context.Context, interaction *discordgo.InteractionCreate, group, command string, options []*discordgo.ApplicationCommandInteractionDataOption) (string, error) {
	switch group {
	case "config":
		return s.handleConfigCommand(ctx, interaction, command, options)
	case "inspect":
		return s.handleInspectCommand(ctx, interaction, command, options)
	default:
		return "", fmt.Errorf("unknown command group")
	}
}

func canUseVoiceCommand(interaction *discordgo.InteractionCreate, botAdminUserIDs []string, group, command string) bool {
	if botauth.IsAllowlisted(interaction, botAdminUserIDs) {
		return true
	}
	if group == "inspect" && (command == "sessions" || command == "session") {
		return hasAdministratorPermissions(interaction)
	}
	if group == "inspect" && (command == voiceInspectHistoryCommandName || command == voiceInspectRecentSessionCommandName) {
		return hasAdministratorPermissions(interaction)
	}
	return hasManagePermissions(interaction)
}

func (s *Service) handleConfigCommand(ctx context.Context, interaction *discordgo.InteractionCreate, command string, options []*discordgo.ApplicationCommandInteractionDataOption) (string, error) {
	switch command {
	case "mode":
		action := optionString(options, "action")
		switch action {
		case "show":
			settings, err := s.GetGuildSettings(ctx, interaction.GuildID)
			if err != nil {
				return "", err
			}
			return fmt.Sprintf("tracking mode: %s", domain.NormalizeTrackingMode(settings.TrackingMode)), nil
		case "set":
			mode := optionString(options, "value")
			if mode == "" {
				return "", fmt.Errorf("mode value is required")
			}
			settings, err := s.SetTrackingMode(ctx, interaction.GuildID, mode)
			if err != nil {
				return "", err
			}
			return s.DescribeSettings(settings), nil
		default:
			return "", fmt.Errorf("unknown mode action")
		}
	case "channels":
		action := optionString(options, "action")
		switch action {
		case "add":
			channelID, err := resolveCommandChannel(interaction, options, "channel", discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice)
			if err != nil {
				return "", err
			}
			settings, err := s.AddTrackedChannel(ctx, interaction.GuildID, channelID)
			if err != nil {
				return "", err
			}
			return s.DescribeSettings(settings), nil
		case "remove":
			channelID, err := resolveCommandChannel(interaction, options, "channel", discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice)
			if err != nil {
				return "", err
			}
			settings, err := s.RemoveTrackedChannel(ctx, interaction.GuildID, channelID)
			if err != nil {
				return "", err
			}
			return s.DescribeSettings(settings), nil
		case "list":
			settings, err := s.ListTrackedChannels(ctx, interaction.GuildID)
			if err != nil {
				return "", err
			}
			return s.DescribeSettings(settings), nil
		case "clear":
			settings, err := s.ClearTrackedChannels(ctx, interaction.GuildID)
			if err != nil {
				return "", err
			}
			return s.DescribeSettings(settings), nil
		default:
			return "", fmt.Errorf("unknown channels action")
		}
	case "summary-channel":
		action := optionString(options, "action")
		switch action {
		case "set":
			channelID, err := resolveCommandChannel(interaction, options, "channel", discordgo.ChannelTypeGuildText)
			if err != nil {
				return "", err
			}
			settings, err := s.SetSummaryChannel(ctx, interaction.GuildID, channelID)
			if err != nil {
				return "", err
			}
			return s.DescribeSettings(settings), nil
		case "clear":
			settings, err := s.ClearSummaryChannel(ctx, interaction.GuildID)
			if err != nil {
				return "", err
			}
			return s.DescribeSettings(settings), nil
		default:
			return "", fmt.Errorf("unknown summary-channel action")
		}
	default:
		return "", fmt.Errorf("unknown config command")
	}
}

func (s *Service) handleInspectCommand(ctx context.Context, interaction *discordgo.InteractionCreate, command string, options []*discordgo.ApplicationCommandInteractionDataOption) (string, error) {
	switch command {
	case "settings":
		settings, err := s.GetGuildSettings(ctx, interaction.GuildID)
		if err != nil {
			return "", err
		}
		return s.DescribeSettings(settings), nil
	case "sessions":
		return s.DescribeActiveSessions(ctx, interaction.GuildID)
	case "session":
		channelID, err := resolveCommandChannel(interaction, options, "channel", discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice)
		if err != nil {
			return "", err
		}
		return s.DescribeActiveSession(ctx, interaction.GuildID, channelID)
	case voiceInspectHistoryCommandName:
		channelID, err := resolveCommandChannel(interaction, options, "channel", discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice)
		if err != nil {
			return "", err
		}
		limit, err := optionIntInRange(options, "limit", 5, 1, 10)
		if err != nil {
			return "", err
		}
		return s.DescribeClosedSessionHistory(ctx, interaction.GuildID, channelID, limit)
	case voiceInspectRecentSessionCommandName:
		channelID, err := resolveCommandChannel(interaction, options, "channel", discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice)
		if err != nil {
			return "", err
		}
		pick, err := optionIntInRange(options, "pick", 1, 1, 10)
		if err != nil {
			return "", err
		}
		return s.DescribeClosedSessionDetail(ctx, interaction.GuildID, channelID, pick)
	default:
		return "", fmt.Errorf("unknown inspect command")
	}
}

func parseVoiceRoute(options []*discordgo.ApplicationCommandInteractionDataOption) (group, command string, leaf []*discordgo.ApplicationCommandInteractionDataOption) {
	if len(options) == 0 {
		return "", "", nil
	}
	groupOpt := options[0]
	group = groupOpt.Name
	if len(groupOpt.Options) == 0 {
		return group, "", nil
	}
	commandOpt := groupOpt.Options[0]
	command = commandOpt.Name
	return group, command, commandOpt.Options
}

func optionString(options []*discordgo.ApplicationCommandInteractionDataOption, name string) string {
	for _, option := range options {
		if option.Name == name {
			if value, ok := option.Value.(string); ok {
				return strings.TrimSpace(value)
			}
		}
	}
	return ""
}

func optionChannelID(options []*discordgo.ApplicationCommandInteractionDataOption, name string) string {
	return optionString(options, name)
}

func optionIntInRange(options []*discordgo.ApplicationCommandInteractionDataOption, name string, fallback, min, max int) (int, error) {
	if fallback < min || fallback > max {
		return 0, fmt.Errorf("invalid default for %s", name)
	}
	for _, option := range options {
		if option.Name != name {
			continue
		}
		switch value := option.Value.(type) {
		case int:
			if value < min || value > max {
				return 0, fmt.Errorf("%s must be between %d and %d", name, min, max)
			}
			return value, nil
		case int32:
			intValue := int(value)
			if intValue < min || intValue > max {
				return 0, fmt.Errorf("%s must be between %d and %d", name, min, max)
			}
			return intValue, nil
		case int64:
			intValue := int(value)
			if intValue < min || intValue > max {
				return 0, fmt.Errorf("%s must be between %d and %d", name, min, max)
			}
			return intValue, nil
		case float64:
			if math.Trunc(value) != value {
				return 0, fmt.Errorf("%s must be a whole number", name)
			}
			intValue := int(value)
			if intValue < min || intValue > max {
				return 0, fmt.Errorf("%s must be between %d and %d", name, min, max)
			}
			return intValue, nil
		case float32:
			if float32(math.Trunc(float64(value))) != value {
				return 0, fmt.Errorf("%s must be a whole number", name)
			}
			intValue := int(value)
			if intValue < min || intValue > max {
				return 0, fmt.Errorf("%s must be between %d and %d", name, min, max)
			}
			return intValue, nil
		default:
			return 0, fmt.Errorf("invalid %s value", name)
		}
	}
	return fallback, nil
}

func resolveCommandChannel(interaction *discordgo.InteractionCreate, options []*discordgo.ApplicationCommandInteractionDataOption, name string, allowedTypes ...discordgo.ChannelType) (string, error) {
	channelID := optionChannelID(options, name)
	if channelID == "" {
		return "", fmt.Errorf("channel is required")
	}
	resolved := interaction.ApplicationCommandData().Resolved
	if resolved == nil || resolved.Channels == nil {
		return "", fmt.Errorf("channel resolution unavailable")
	}
	channel, ok := resolved.Channels[channelID]
	if !ok {
		return "", fmt.Errorf("unable to resolve channel")
	}
	if channel.GuildID != "" && channel.GuildID != interaction.GuildID {
		return "", fmt.Errorf("channel must belong to this guild")
	}
	if len(allowedTypes) > 0 && !channelTypeAllowed(channel.Type, allowedTypes...) {
		return "", fmt.Errorf("unsupported channel type")
	}
	return channelID, nil
}

func channelTypeAllowed(channelType discordgo.ChannelType, allowedTypes ...discordgo.ChannelType) bool {
	for _, allowed := range allowedTypes {
		if channelType == allowed {
			return true
		}
	}
	return false
}

func hasManagePermissions(interaction *discordgo.InteractionCreate) bool {
	if interaction == nil || interaction.Member == nil {
		return false
	}
	permissions := interaction.Member.Permissions
	return permissions&(discordgo.PermissionAdministrator|discordgo.PermissionManageGuild) != 0
}

func hasAdministratorPermissions(interaction *discordgo.InteractionCreate) bool {
	if interaction == nil || interaction.Member == nil {
		return false
	}
	return interaction.Member.Permissions&discordgo.PermissionAdministrator != 0
}

func respondEphemeral(session *discordgo.Session, interaction *discordgo.InteractionCreate, content string) error {
	return session.InteractionRespond(interaction.Interaction, &discordgo.InteractionResponse{
		Type: discordgo.InteractionResponseChannelMessageWithSource,
		Data: &discordgo.InteractionResponseData{
			Flags: discordgo.MessageFlagsEphemeral,
			Embeds: []*discordgo.MessageEmbed{{
				Title:       "Voice Tracker",
				Description: content,
				Color:       0x5865F2,
			}},
		},
	})
}
