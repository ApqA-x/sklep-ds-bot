package shuffle

import (
	"context"
	"fmt"
	"math/rand"
	"strings"
	"time"

	"github.com/bwmarrin/discordgo"
)

type Mover interface {
	GuildMember(guildID, userID string, options ...discordgo.RequestOption) (*discordgo.Member, error)
	GuildMemberMove(guildID string, userID string, channelID *string, options ...discordgo.RequestOption) error
	UserChannelPermissions(userID, channelID string, fetchOptions ...discordgo.RequestOption) (int64, error)
}

type Service struct {
	state     *discordgo.State
	mover     Mover
	botUserID string
	rng       *rand.Rand
}

type ChannelResult struct {
	ChannelID string `json:"channelId"`
	Moved     int    `json:"moved"`
}

type Result struct {
	MovableUsers      int             `json:"movableUsers"`
	MovedUsers        int             `json:"movedUsers"`
	ExcludedUsers     int             `json:"excludedUsers"`
	SkippedChannels   int             `json:"skippedChannels"`
	SkippedChannelIDs []string        `json:"skippedChannelIds,omitempty"`
	ChannelResults    []ChannelResult `json:"channelResults"`
	Failures          []string        `json:"failures,omitempty"`
}

func New(state *discordgo.State, mover Mover, botUserID string, rng *rand.Rand) *Service {
	if rng == nil {
		rng = rand.New(rand.NewSource(time.Now().UTC().UnixNano()))
	}
	return &Service{state: state, mover: mover, botUserID: strings.TrimSpace(botUserID), rng: rng}
}

func (s *Service) Equal(ctx context.Context, guildID string, channelIDs, excludedIDs []string) (Result, error) {
	if s == nil || s.state == nil || s.mover == nil {
		return Result{}, fmt.Errorf("shuffle service is unavailable")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	guildID = strings.TrimSpace(guildID)
	if guildID == "" {
		return Result{}, fmt.Errorf("guild id is required")
	}

	guild, err := s.state.Guild(guildID)
	if err != nil {
		return Result{}, fmt.Errorf("load guild state: %w", err)
	}
	if guild == nil {
		return Result{}, fmt.Errorf("guild state is unavailable")
	}

	targetChannels, err := s.normalizeChannels(guild, channelIDs)
	if err != nil {
		return Result{}, err
	}
	if len(targetChannels) < 2 {
		return Result{}, fmt.Errorf("at least two voice channels are required")
	}
	if err := s.ensurePermissions(ctx, targetChannels); err != nil {
		return Result{}, err
	}

	excludedSet := make(map[string]struct{}, len(excludedIDs))
	for _, id := range normalizeIDs(excludedIDs) {
		excludedSet[id] = struct{}{}
	}

	users, excludedCount := s.collectUsers(ctx, guildID, guild, targetChannels, excludedSet)
	if len(users) < len(targetChannels) {
		return Result{}, fmt.Errorf("not enough people to shuffle: need at least %d movable users for %d channels", len(targetChannels), len(targetChannels))
	}

	s.shuffleStrings(users)
	shuffledChannels := append([]string(nil), targetChannels...)
	s.shuffleStrings(shuffledChannels)

	counts := balancedCounts(len(users), len(shuffledChannels))
	result := Result{
		MovableUsers:   len(users),
		ExcludedUsers:  excludedCount,
		ChannelResults: make([]ChannelResult, 0, len(shuffledChannels)),
	}

	remaining := append([]string(nil), users...)
	for i, channelID := range shuffledChannels {
		channelResult := ChannelResult{ChannelID: channelID}
		slots := counts[i]
		for slots > 0 {
			if err := ctx.Err(); err != nil {
				return result, err
			}
			if len(remaining) == 0 {
				break
			}
			userID := remaining[0]
			remaining = remaining[1:]
			targetChannelID := channelID
			if err := s.mover.GuildMemberMove(guildID, userID, &targetChannelID, discordgo.WithContext(ctx)); err != nil {
				result.Failures = append(result.Failures, fmt.Sprintf("<@%s> -> <#%s>: %v", userID, channelID, err))
				continue
			}
			channelResult.Moved++
			result.MovedUsers++
			slots--
		}
		result.ChannelResults = append(result.ChannelResults, channelResult)
	}

	return result, nil
}

func (s *Service) Gather(ctx context.Context, guildID, destinationChannelID string, sourceChannelIDs, excludedIDs []string) (Result, error) {
	if s == nil || s.state == nil || s.mover == nil {
		return Result{}, fmt.Errorf("shuffle service is unavailable")
	}
	if ctx == nil {
		ctx = context.Background()
	}
	guildID = strings.TrimSpace(guildID)
	if guildID == "" {
		return Result{}, fmt.Errorf("guild id is required")
	}
	destinationChannelID = strings.TrimSpace(destinationChannelID)
	if destinationChannelID == "" {
		return Result{}, fmt.Errorf("destination channel is required")
	}

	guild, err := s.state.Guild(guildID)
	if err != nil {
		return Result{}, fmt.Errorf("load guild state: %w", err)
	}
	if guild == nil {
		return Result{}, fmt.Errorf("guild state is unavailable")
	}

	destinationChannel, err := findChannel(guild, destinationChannelID)
	if err != nil {
		return Result{}, fmt.Errorf("unable to resolve destination channel %s", destinationChannelID)
	}
	if !channelTypeAllowed(destinationChannel.Type, discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice) {
		return Result{}, fmt.Errorf("unsupported channel type for <#%s>", destinationChannelID)
	}

	var sourceChannels []string
	if len(sourceChannelIDs) == 0 {
		sourceChannels = s.allVoiceChannels(guild, destinationChannelID)
	} else {
		sourceChannels, err = s.normalizeChannels(guild, sourceChannelIDs)
		if err != nil {
			return Result{}, err
		}
		sourceChannels = withoutChannel(sourceChannels, destinationChannelID)
	}
	if len(sourceChannels) == 0 {
		return Result{}, fmt.Errorf("at least one source voice channel is required")
	}

	if err := s.ensurePermissions(ctx, []string{destinationChannelID}); err != nil {
		return Result{}, err
	}
	accessibleSources, skippedChannels, skippedChannelIDs, err := s.filterAccessibleChannels(ctx, sourceChannels)
	if err != nil {
		return Result{}, err
	}
	if len(accessibleSources) == 0 {
		return Result{}, fmt.Errorf("no accessible voice channels to gather from")
	}

	excludedSet := make(map[string]struct{}, len(excludedIDs))
	for _, id := range normalizeIDs(excludedIDs) {
		excludedSet[id] = struct{}{}
	}

	users, excludedCount := s.collectUsers(ctx, guildID, guild, accessibleSources, excludedSet)
	if len(users) == 0 {
		return Result{}, fmt.Errorf("no movable users to gather")
	}

	s.shuffleStrings(users)
	result := Result{
		MovableUsers:      len(users),
		ExcludedUsers:     excludedCount,
		SkippedChannels:   skippedChannels,
		SkippedChannelIDs: skippedChannelIDs,
		ChannelResults: []ChannelResult{{
			ChannelID: destinationChannelID,
		}},
	}

	for _, userID := range users {
		if err := ctx.Err(); err != nil {
			return result, err
		}
		targetChannelID := destinationChannelID
		if err := s.mover.GuildMemberMove(guildID, userID, &targetChannelID, discordgo.WithContext(ctx)); err != nil {
			result.Failures = append(result.Failures, fmt.Sprintf("<@%s> -> <#%s>: %v", userID, destinationChannelID, err))
			continue
		}
		result.ChannelResults[0].Moved++
		result.MovedUsers++
	}

	return result, nil
}

func (s *Service) normalizeChannels(guild *discordgo.Guild, channelIDs []string) ([]string, error) {
	seen := make(map[string]struct{}, len(channelIDs))
	out := make([]string, 0, len(channelIDs))
	for _, raw := range channelIDs {
		channelID := strings.TrimSpace(raw)
		if channelID == "" {
			continue
		}
		if _, ok := seen[channelID]; ok {
			return nil, fmt.Errorf("duplicate channel %s", channelID)
		}
		channel, err := findChannel(guild, channelID)
		if err != nil {
			return nil, fmt.Errorf("unable to resolve channel %s", channelID)
		}
		if !channelTypeAllowed(channel.Type, discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice) {
			return nil, fmt.Errorf("unsupported channel type for <#%s>", channelID)
		}
		seen[channelID] = struct{}{}
		out = append(out, channelID)
	}
	return out, nil
}

func (s *Service) allVoiceChannels(guild *discordgo.Guild, excludeIDs ...string) []string {
	excluded := make(map[string]struct{}, len(excludeIDs))
	for _, id := range normalizeIDs(excludeIDs) {
		excluded[id] = struct{}{}
	}
	channels := make([]string, 0, len(guild.Channels))
	for _, channel := range guild.Channels {
		if channel == nil {
			continue
		}
		if _, ok := excluded[channel.ID]; ok {
			continue
		}
		if !channelTypeAllowed(channel.Type, discordgo.ChannelTypeGuildVoice, discordgo.ChannelTypeGuildStageVoice) {
			continue
		}
		channels = append(channels, channel.ID)
	}
	return channels
}

func (s *Service) ensurePermissions(ctx context.Context, channelIDs []string) error {
	if s.botUserID == "" {
		return fmt.Errorf("bot user id is unavailable")
	}
	options := []discordgo.RequestOption{discordgo.WithContext(ctx)}
	for _, channelID := range channelIDs {
		perms, err := s.mover.UserChannelPermissions(s.botUserID, channelID, options...)
		if err != nil {
			return fmt.Errorf("check permissions for <#%s>: %w", channelID, err)
		}
		if perms&discordgo.PermissionViewChannel == 0 {
			return fmt.Errorf("missing view channel permission for <#%s>", channelID)
		}
		if perms&discordgo.PermissionVoiceConnect == 0 {
			return fmt.Errorf("missing connect permission for <#%s>", channelID)
		}
		if perms&discordgo.PermissionVoiceMoveMembers == 0 {
			return fmt.Errorf("missing move members permission for <#%s>", channelID)
		}
	}
	return nil
}

func (s *Service) filterAccessibleChannels(ctx context.Context, channelIDs []string) ([]string, int, []string, error) {
	if s.botUserID == "" {
		return nil, 0, nil, fmt.Errorf("bot user id is unavailable")
	}
	options := []discordgo.RequestOption{discordgo.WithContext(ctx)}
	accessible := make([]string, 0, len(channelIDs))
	skippedIDs := make([]string, 0)
	skipped := 0
	for _, channelID := range normalizeIDs(channelIDs) {
		perms, err := s.mover.UserChannelPermissions(s.botUserID, channelID, options...)
		if err != nil {
			return nil, 0, nil, fmt.Errorf("check permissions for <#%s>: %w", channelID, err)
		}
		if perms&discordgo.PermissionViewChannel == 0 || perms&discordgo.PermissionVoiceConnect == 0 || perms&discordgo.PermissionVoiceMoveMembers == 0 {
			skipped++
			skippedIDs = append(skippedIDs, channelID)
			continue
		}
		accessible = append(accessible, channelID)
	}
	return accessible, skipped, skippedIDs, nil
}

func (s *Service) collectUsers(ctx context.Context, guildID string, guild *discordgo.Guild, channelIDs []string, excludedSet map[string]struct{}) ([]string, int) {
	targets := make(map[string]struct{}, len(channelIDs))
	for _, channelID := range channelIDs {
		targets[channelID] = struct{}{}
	}
	users := make([]string, 0)
	seen := make(map[string]struct{})
	excludedCount := 0
	for _, voiceState := range guild.VoiceStates {
		if _, ok := targets[voiceState.ChannelID]; !ok {
			continue
		}
		userID := strings.TrimSpace(voiceState.UserID)
		if userID == "" || userID == s.botUserID {
			continue
		}
		if _, ok := excludedSet[userID]; ok {
			excludedCount++
			continue
		}
		if _, ok := seen[userID]; ok {
			continue
		}
		if s.isBot(ctx, guildID, userID) {
			continue
		}
		seen[userID] = struct{}{}
		users = append(users, userID)
	}
	return users, excludedCount
}

func (s *Service) isBot(ctx context.Context, guildID, userID string) bool {
	if s.state != nil {
		if member, err := s.state.Member(guildID, userID); err == nil && member != nil && member.User != nil {
			return member.User.Bot
		}
	}
	member, err := s.mover.GuildMember(guildID, userID, discordgo.WithContext(ctx))
	if err != nil || member == nil || member.User == nil {
		return false
	}
	return member.User.Bot
}

func (s *Service) shuffleStrings(values []string) {
	if len(values) < 2 {
		return
	}
	s.rng.Shuffle(len(values), func(i, j int) {
		values[i], values[j] = values[j], values[i]
	})
}

func balancedCounts(total, buckets int) []int {
	counts := make([]int, buckets)
	if buckets == 0 {
		return counts
	}
	base := total / buckets
	extra := total % buckets
	for i := 0; i < buckets; i++ {
		counts[i] = base
		if i < extra {
			counts[i]++
		}
	}
	return counts
}

func normalizeIDs(values []string) []string {
	seen := make(map[string]struct{}, len(values))
	out := make([]string, 0, len(values))
	for _, raw := range values {
		value := strings.TrimSpace(raw)
		if value == "" {
			continue
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		out = append(out, value)
	}
	return out
}

func withoutChannel(values []string, channelID string) []string {
	channelID = strings.TrimSpace(channelID)
	if channelID == "" {
		return append([]string(nil), values...)
	}
	out := make([]string, 0, len(values))
	for _, value := range values {
		if strings.TrimSpace(value) == channelID {
			continue
		}
		out = append(out, value)
	}
	return out
}

func findChannel(guild *discordgo.Guild, channelID string) (*discordgo.Channel, error) {
	if guild == nil {
		return nil, fmt.Errorf("guild is unavailable")
	}
	for _, channel := range guild.Channels {
		if channel != nil && channel.ID == channelID {
			return channel, nil
		}
	}
	return nil, fmt.Errorf("channel not found")
}

func channelTypeAllowed(channelType discordgo.ChannelType, allowedTypes ...discordgo.ChannelType) bool {
	for _, allowed := range allowedTypes {
		if channelType == allowed {
			return true
		}
	}
	return false
}
