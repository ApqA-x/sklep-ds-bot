package shuffle

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"
	"unicode"

	"github.com/bwmarrin/discordgo"
	"github.com/robinlant/sklep-ds-bot/internal/botauth"
)

const (
	shuffleCommandName = "shuffle"
	shuffleGatherGroup = "gather"
	shuffleEqualGroup  = "equal"
)

func ShuffleApplicationCommand() *discordgo.ApplicationCommand {
	return &discordgo.ApplicationCommand{
		Name:        shuffleCommandName,
		Description: "Redistribute people evenly across voice channels",
		Options: []*discordgo.ApplicationCommandOption{
			{
				Type:        discordgo.ApplicationCommandOptionSubCommandGroup,
				Name:        shuffleGatherGroup,
				Description: "Put everyone back into one voice channel",
				Options: []*discordgo.ApplicationCommandOption{
					shuffleGatherAllCommand(),
					shuffleGatherSelectCommand(),
				},
			},
			{
				Type:        discordgo.ApplicationCommandOptionSubCommandGroup,
				Name:        shuffleEqualGroup,
				Description: "Evenly reshuffle voice channels",
				Options: []*discordgo.ApplicationCommandOption{
					shuffleEqualCommand("two", 2),
					shuffleEqualCommand("three", 3),
					shuffleEqualCommand("four", 4),
				},
			},
		},
	}
}

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
		if data.Name != shuffleCommandName {
			return
		}

		group, command, options := parseShuffleRoute(data.Options)
		if group != shuffleEqualGroup && group != shuffleGatherGroup {
			_ = respondEphemeral(ds, interaction, "Unknown shuffle command.")
			return
		}
		if !canUseShuffleCommand(interaction, botAdminUserIDs) {
			_ = respondEphemeral(ds, interaction, "Insufficient permissions.")
			return
		}
		if err := deferEphemeral(ds, interaction); err != nil {
			return
		}

		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()

		var content string
		var err error
		switch group {
		case shuffleEqualGroup:
			content, err = s.handleEqualCommand(ctx, interaction, command, options)
		case shuffleGatherGroup:
			content, err = s.handleGatherCommand(ctx, interaction, command, options)
		}
		if err != nil {
			content = err.Error()
		}
		if content == "" {
			content = "Done."
		}
		_ = editEphemeral(ds, interaction, content)
	})
}

func shuffleEqualCommand(name string, channels int) *discordgo.ApplicationCommandOption {
	options := make([]*discordgo.ApplicationCommandOption, 0, channels+1)
	for i := 1; i <= channels; i++ {
		options = append(options, &discordgo.ApplicationCommandOption{
			Type:         discordgo.ApplicationCommandOptionChannel,
			Name:         fmt.Sprintf("voice%d", i),
			Description:  fmt.Sprintf("Voice channel %d", i),
			Required:     true,
			ChannelTypes: []discordgo.ChannelType{discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice},
		})
	}
	options = append(options, &discordgo.ApplicationCommandOption{
		Type:        discordgo.ApplicationCommandOptionString,
		Name:        "exclude",
		Description: "User IDs or mentions to keep in place",
	})
	return &discordgo.ApplicationCommandOption{
		Type:        discordgo.ApplicationCommandOptionSubCommand,
		Name:        name,
		Description: fmt.Sprintf("Reshuffle %d voice channels", channels),
		Options:     options,
	}
}

func shuffleGatherAllCommand() *discordgo.ApplicationCommandOption {
	return &discordgo.ApplicationCommandOption{
		Type:        discordgo.ApplicationCommandOptionSubCommand,
		Name:        "all",
		Description: "Gather everyone from every voice channel into one channel",
		Options: []*discordgo.ApplicationCommandOption{
			{
				Type:         discordgo.ApplicationCommandOptionChannel,
				Name:         "destination",
				Description:  "Voice channel to gather into",
				Required:     true,
				ChannelTypes: []discordgo.ChannelType{discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice},
			},
			{
				Type:        discordgo.ApplicationCommandOptionString,
				Name:        "exclude",
				Description: "User IDs or mentions to keep in place",
			},
		},
	}
}

func shuffleGatherSelectCommand() *discordgo.ApplicationCommandOption {
	options := []*discordgo.ApplicationCommandOption{
		{
			Type:         discordgo.ApplicationCommandOptionChannel,
			Name:         "destination",
			Description:  "Voice channel to gather into",
			Required:     true,
			ChannelTypes: []discordgo.ChannelType{discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice},
		},
		{
			Type:         discordgo.ApplicationCommandOptionChannel,
			Name:         "source1",
			Description:  "Voice channel 1",
			Required:     true,
			ChannelTypes: []discordgo.ChannelType{discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice},
		},
	}
	for i := 2; i <= 8; i++ {
		options = append(options, &discordgo.ApplicationCommandOption{
			Type:         discordgo.ApplicationCommandOptionChannel,
			Name:         fmt.Sprintf("source%d", i),
			Description:  fmt.Sprintf("Voice channel %d", i),
			ChannelTypes: []discordgo.ChannelType{discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice},
		})
	}
	options = append(options, &discordgo.ApplicationCommandOption{
		Type:        discordgo.ApplicationCommandOptionString,
		Name:        "exclude",
		Description: "User IDs or mentions to keep in place",
	})
	return &discordgo.ApplicationCommandOption{
		Type:        discordgo.ApplicationCommandOptionSubCommand,
		Name:        "select",
		Description: "Gather members from chosen voice channels into one channel",
		Options:     options,
	}
}

func (s *Service) handleEqualCommand(ctx context.Context, interaction *discordgo.InteractionCreate, command string, options []*discordgo.ApplicationCommandInteractionDataOption) (string, error) {
	switch command {
	case "two", "three", "four":
		channelIDs, err := resolveShuffleChannels(interaction, options, command)
		if err != nil {
			return "", err
		}
		excludedIDs, err := parseExcludedUserIDs(optionString(options, "exclude"))
		if err != nil {
			return "", err
		}
		result, err := s.Equal(ctx, interaction.GuildID, channelIDs, excludedIDs)
		if err != nil {
			return "", err
		}
		return formatShuffleResult(result), nil
	default:
		return "", fmt.Errorf("unknown shuffle equal command")
	}
}

func (s *Service) handleGatherCommand(ctx context.Context, interaction *discordgo.InteractionCreate, command string, options []*discordgo.ApplicationCommandInteractionDataOption) (string, error) {
	switch command {
	case "all":
		destinationChannelID, err := resolveCommandChannel(interaction, options, "destination", discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice)
		if err != nil {
			return "", err
		}
		excludedIDs, err := parseExcludedUserIDs(optionString(options, "exclude"))
		if err != nil {
			return "", err
		}
		result, err := s.Gather(ctx, interaction.GuildID, destinationChannelID, nil, excludedIDs)
		if err != nil {
			return "", err
		}
		return formatGatherResult(destinationChannelID, result), nil
	case "select":
		destinationChannelID, err := resolveCommandChannel(interaction, options, "destination", discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice)
		if err != nil {
			return "", err
		}
		sourceChannelIDs, err := resolveGatherChannels(interaction, options)
		if err != nil {
			return "", err
		}
		excludedIDs, err := parseExcludedUserIDs(optionString(options, "exclude"))
		if err != nil {
			return "", err
		}
		result, err := s.Gather(ctx, interaction.GuildID, destinationChannelID, sourceChannelIDs, excludedIDs)
		if err != nil {
			return "", err
		}
		return formatGatherResult(destinationChannelID, result), nil
	default:
		return "", fmt.Errorf("unknown shuffle gather command")
	}
}

func formatShuffleResult(result Result) string {
	var builder strings.Builder
	if len(result.Failures) == 0 {
		builder.WriteString(fmt.Sprintf("Shuffled %d users across %d channels.\n", result.MovedUsers, len(result.ChannelResults)))
	} else {
		builder.WriteString(fmt.Sprintf("Shuffled %d users across %d channels with %d move failure(s).\n", result.MovedUsers, len(result.ChannelResults), len(result.Failures)))
	}
	if result.ExcludedUsers > 0 {
		builder.WriteString(fmt.Sprintf("Excluded %d user(s).\n", result.ExcludedUsers))
	}
	for _, channel := range result.ChannelResults {
		builder.WriteString(fmt.Sprintf("<#%s>: %d moved\n", channel.ChannelID, channel.Moved))
	}
	if len(result.Failures) > 0 {
		builder.WriteString("Failures:\n")
		for _, failure := range result.Failures {
			builder.WriteString(fmt.Sprintf("- %s\n", failure))
		}
	}
	return strings.TrimSpace(builder.String())
}

func formatGatherResult(destinationChannelID string, result Result) string {
	var builder strings.Builder
	if len(result.Failures) == 0 {
		builder.WriteString(fmt.Sprintf("Gathered %d users into <#%s>.\n", result.MovedUsers, destinationChannelID))
	} else {
		builder.WriteString(fmt.Sprintf("Gathered %d users into <#%s> with %d move failure(s).\n", result.MovedUsers, destinationChannelID, len(result.Failures)))
	}
	if result.SkippedChannels > 0 {
		builder.WriteString(fmt.Sprintf("Skipped %d inaccessible channel(s): %s.\n", result.SkippedChannels, formatChannelMentions(result.SkippedChannelIDs)))
	}
	if result.ExcludedUsers > 0 {
		builder.WriteString(fmt.Sprintf("Excluded %d user(s).\n", result.ExcludedUsers))
	}
	if len(result.ChannelResults) > 0 {
		builder.WriteString(fmt.Sprintf("<#%s>: %d moved\n", result.ChannelResults[0].ChannelID, result.ChannelResults[0].Moved))
	}
	if len(result.Failures) > 0 {
		builder.WriteString("Failures:\n")
		for _, failure := range result.Failures {
			builder.WriteString(fmt.Sprintf("- %s\n", failure))
		}
	}
	return strings.TrimSpace(builder.String())
}

func formatChannelMentions(channelIDs []string) string {
	if len(channelIDs) == 0 {
		return "none"
	}
	mentions := make([]string, 0, len(channelIDs))
	for _, channelID := range channelIDs {
		if strings.TrimSpace(channelID) == "" {
			continue
		}
		mentions = append(mentions, fmt.Sprintf("<#%s>", channelID))
	}
	if len(mentions) == 0 {
		return "none"
	}
	return strings.Join(mentions, ", ")
}

func canUseShuffleCommand(interaction *discordgo.InteractionCreate, botAdminUserIDs []string) bool {
	if botauth.IsAllowlisted(interaction, botAdminUserIDs) {
		return true
	}
	if interaction == nil || interaction.Member == nil {
		return false
	}
	permissions := interaction.Member.Permissions
	return permissions&(discordgo.PermissionAdministrator|discordgo.PermissionVoiceMoveMembers) != 0
}

func parseShuffleRoute(options []*discordgo.ApplicationCommandInteractionDataOption) (group, command string, leaf []*discordgo.ApplicationCommandInteractionDataOption) {
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

func resolveShuffleChannels(interaction *discordgo.InteractionCreate, options []*discordgo.ApplicationCommandInteractionDataOption, command string) ([]string, error) {
	required := map[string]struct{}{}
	switch command {
	case "two":
		required = map[string]struct{}{"voice1": {}, "voice2": {}}
	case "three":
		required = map[string]struct{}{"voice1": {}, "voice2": {}, "voice3": {}}
	case "four":
		required = map[string]struct{}{"voice1": {}, "voice2": {}, "voice3": {}, "voice4": {}}
	default:
		return nil, fmt.Errorf("unknown shuffle equal command")
	}
	resolved := make([]string, 0, len(required))
	for _, name := range []string{"voice1", "voice2", "voice3", "voice4"} {
		if _, ok := required[name]; !ok {
			continue
		}
		channelID, err := resolveCommandChannel(interaction, options, name, discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice)
		if err != nil {
			return nil, err
		}
		resolved = append(resolved, channelID)
	}
	return resolved, nil
}

func resolveGatherChannels(interaction *discordgo.InteractionCreate, options []*discordgo.ApplicationCommandInteractionDataOption) ([]string, error) {
	resolved := make([]string, 0, 8)
	for i := 1; i <= 8; i++ {
		name := fmt.Sprintf("source%d", i)
		channelID, err := resolveCommandChannel(interaction, options, name, discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice)
		if err != nil {
			if optionChannelID(options, name) == "" {
				continue
			}
			return nil, err
		}
		resolved = append(resolved, channelID)
	}
	if len(resolved) == 0 {
		return nil, fmt.Errorf("at least one source channel is required")
	}
	return resolved, nil
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
	for _, option := range options {
		if option.Name == name {
			if value, ok := option.Value.(string); ok {
				return strings.TrimSpace(value)
			}
		}
	}
	return ""
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

func parseExcludedUserIDs(raw string) ([]string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	tokens := strings.FieldsFunc(raw, func(r rune) bool {
		return unicode.IsSpace(r) || r == ',' || r == ';'
	})
	ids := make([]string, 0, len(tokens))
	seen := make(map[string]struct{}, len(tokens))
	for _, token := range tokens {
		id, err := parseUserIDToken(token)
		if err != nil {
			return nil, err
		}
		if _, ok := seen[id]; ok {
			continue
		}
		seen[id] = struct{}{}
		ids = append(ids, id)
	}
	return ids, nil
}

func parseUserIDToken(token string) (string, error) {
	token = strings.TrimSpace(token)
	if token == "" {
		return "", fmt.Errorf("invalid excluded user")
	}
	if strings.HasPrefix(token, "<@") && strings.HasSuffix(token, ">") {
		token = strings.TrimPrefix(token, "<@!")
		token = strings.TrimPrefix(token, "<@")
		token = strings.TrimSuffix(token, ">")
	}
	if _, err := strconv.ParseUint(token, 10, 64); err != nil {
		return "", fmt.Errorf("invalid excluded user %q", token)
	}
	return token, nil
}

func respondEphemeral(session *discordgo.Session, interaction *discordgo.InteractionCreate, content string) error {
	return session.InteractionRespond(interaction.Interaction, &discordgo.InteractionResponse{
		Type: discordgo.InteractionResponseChannelMessageWithSource,
		Data: &discordgo.InteractionResponseData{Content: content, Flags: discordgo.MessageFlagsEphemeral},
	})
}

func deferEphemeral(session *discordgo.Session, interaction *discordgo.InteractionCreate) error {
	return session.InteractionRespond(interaction.Interaction, &discordgo.InteractionResponse{
		Type: discordgo.InteractionResponseDeferredChannelMessageWithSource,
		Data: &discordgo.InteractionResponseData{Flags: discordgo.MessageFlagsEphemeral},
	})
}

func editEphemeral(session *discordgo.Session, interaction *discordgo.InteractionCreate, content string) error {
	_, err := session.InteractionResponseEdit(interaction.Interaction, &discordgo.WebhookEdit{Content: &content})
	return err
}
